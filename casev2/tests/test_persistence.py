import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.engine import Engine, Order
from app.engine.persistence import (
    NewOrderItem,
    TradeItem,
    OrderUpdateItem,
    flush_batch,
)
from app.models import OrderSide, OrderType, OrderStatus


@pytest_asyncio.fixture
async def db_engine():
    eng = create_async_engine(settings.database_url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def clean_db(session_factory):
    """Clean all tables before test."""
    async with session_factory() as session:
        await session.execute(text("DELETE FROM trades"))
        await session.execute(text("DELETE FROM orders"))
        await session.execute(text("DELETE FROM brokers"))
        await session.commit()
    yield


@pytest_asyncio.fixture
async def two_brokers(session_factory, clean_db):
    """Create two brokers and return their IDs."""
    broker_a_id = uuid.uuid4()
    broker_b_id = uuid.uuid4()
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO brokers (id, name, api_key_hash) VALUES (:id, :name, :hash)"),
            [
                {"id": str(broker_a_id), "name": "Broker A", "hash": "hash_a"},
                {"id": str(broker_b_id), "name": "Broker B", "hash": "hash_b"},
            ],
        )
        await session.commit()
    return broker_a_id, broker_b_id


@pytest_asyncio.fixture
async def test_engine():
    """Fresh Engine instance for each test."""
    eng = Engine()
    yield eng


class TestFlushBatchInsertOrders:
    @pytest.mark.asyncio
    async def test_insert_single_order(self, session_factory, two_brokers, test_engine):
        broker_a, _ = two_brokers
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()

        items = [
            NewOrderItem(
                id=order_id,
                broker_id=broker_a,
                symbol="PETR4",
                side=OrderSide.bid,
                order_type=OrderType.limit,
                price=1000,
                quantity=100,
                remaining_quantity=100,
                status=OrderStatus.open,
                document_number="12345678901",
                valid_until=now,
                created_at=now,
            ),
        ]

        await flush_batch(items, session_factory, test_engine)

        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id, symbol, price, quantity, status FROM orders WHERE id = :id"),
                {"id": str(order_id)},
            )
            row = result.fetchone()
            assert row is not None
            assert row[1] == "PETR4"
            assert row[2] == 1000
            assert row[3] == 100
            assert row[4] == "open"


class TestFlushBatchInsertTrades:
    @pytest.mark.asyncio
    async def test_insert_trade_and_update_balances(self, session_factory, two_brokers, test_engine):
        broker_a, broker_b = two_brokers
        now = datetime.now(timezone.utc)
        buy_order_id = uuid.uuid4()
        sell_order_id = uuid.uuid4()
        trade_id = uuid.uuid4()

        items = [
            # First insert both orders
            NewOrderItem(
                id=buy_order_id, broker_id=broker_a, symbol="PETR4",
                side=OrderSide.bid, order_type=OrderType.limit, price=1000,
                quantity=100, remaining_quantity=100, status=OrderStatus.open,
                document_number="111", valid_until=now, created_at=now,
            ),
            NewOrderItem(
                id=sell_order_id, broker_id=broker_b, symbol="PETR4",
                side=OrderSide.ask, order_type=OrderType.limit, price=1000,
                quantity=100, remaining_quantity=100, status=OrderStatus.open,
                document_number="222", valid_until=now, created_at=now,
            ),
            # Then trade
            TradeItem(
                id=trade_id, buy_order_id=buy_order_id, sell_order_id=sell_order_id,
                symbol="PETR4", price=1000, quantity=100,
                buyer_broker_id=broker_a, seller_broker_id=broker_b,
                buyer_remaining_qty=0, seller_remaining_qty=0,
                created_at=now,
            ),
            # Then update orders
            OrderUpdateItem(order_id=buy_order_id, status=OrderStatus.closed, remaining_quantity=0),
            OrderUpdateItem(order_id=sell_order_id, status=OrderStatus.closed, remaining_quantity=0),
        ]

        await flush_batch(items, session_factory, test_engine)

        async with session_factory() as session:
            # Verify trade exists
            result = await session.execute(
                text("SELECT price, quantity FROM trades WHERE id = :id"),
                {"id": str(trade_id)},
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == 1000
            assert row[1] == 100

            # Verify balances: buyer -100000, seller +100000
            result = await session.execute(
                text("SELECT balance FROM brokers WHERE id = :id"),
                {"id": str(broker_a)},
            )
            assert result.scalar() == -100000  # 1000 * 100

            result = await session.execute(
                text("SELECT balance FROM brokers WHERE id = :id"),
                {"id": str(broker_b)},
            )
            assert result.scalar() == 100000

            # Verify orders updated to closed
            result = await session.execute(
                text("SELECT status FROM orders WHERE id = :id"),
                {"id": str(buy_order_id)},
            )
            assert result.scalar() == "closed"


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_order_update_dedup_keeps_last(self, session_factory, two_brokers, test_engine):
        broker_a, _ = two_brokers
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()

        items = [
            NewOrderItem(
                id=order_id, broker_id=broker_a, symbol="PETR4",
                side=OrderSide.bid, order_type=OrderType.limit, price=1000,
                quantity=500, remaining_quantity=500, status=OrderStatus.open,
                document_number="111", valid_until=now, created_at=now,
            ),
            # First partial fill
            OrderUpdateItem(order_id=order_id, status=OrderStatus.open, remaining_quantity=300),
            # Second partial fill — this should win
            OrderUpdateItem(order_id=order_id, status=OrderStatus.open, remaining_quantity=100),
        ]

        await flush_batch(items, session_factory, test_engine)

        async with session_factory() as session:
            result = await session.execute(
                text("SELECT remaining_quantity FROM orders WHERE id = :id"),
                {"id": str(order_id)},
            )
            assert result.scalar() == 100  # last update wins


class TestEviction:
    @pytest.mark.asyncio
    async def test_closed_orders_evicted_from_engine(self, session_factory, two_brokers, test_engine):
        broker_a, _ = two_brokers
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()

        # Pre-populate engine.orders
        mem_order = Order(
            id=order_id, broker_id=broker_a, symbol="PETR4",
            side=OrderSide.bid, order_type=OrderType.limit, price=1000,
            quantity=100, remaining_quantity=0, status=OrderStatus.closed,
            document_number="111", valid_until=now, created_at=now,
        )
        test_engine.orders[order_id] = mem_order

        items = [
            NewOrderItem(
                id=order_id, broker_id=broker_a, symbol="PETR4",
                side=OrderSide.bid, order_type=OrderType.limit, price=1000,
                quantity=100, remaining_quantity=100, status=OrderStatus.open,
                document_number="111", valid_until=now, created_at=now,
            ),
            OrderUpdateItem(order_id=order_id, status=OrderStatus.closed, remaining_quantity=0),
        ]

        await flush_batch(items, session_factory, test_engine)

        # Order should be evicted from engine.orders
        assert order_id not in test_engine.orders

    @pytest.mark.asyncio
    async def test_open_orders_not_evicted(self, session_factory, two_brokers, test_engine):
        broker_a, _ = two_brokers
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()

        mem_order = Order(
            id=order_id, broker_id=broker_a, symbol="PETR4",
            side=OrderSide.bid, order_type=OrderType.limit, price=1000,
            quantity=500, remaining_quantity=300, status=OrderStatus.open,
            document_number="111", valid_until=now, created_at=now,
        )
        test_engine.orders[order_id] = mem_order

        items = [
            NewOrderItem(
                id=order_id, broker_id=broker_a, symbol="PETR4",
                side=OrderSide.bid, order_type=OrderType.limit, price=1000,
                quantity=500, remaining_quantity=500, status=OrderStatus.open,
                document_number="111", valid_until=now, created_at=now,
            ),
            OrderUpdateItem(order_id=order_id, status=OrderStatus.open, remaining_quantity=300),
        ]

        await flush_batch(items, session_factory, test_engine)

        # Order should still be in engine.orders
        assert order_id in test_engine.orders
