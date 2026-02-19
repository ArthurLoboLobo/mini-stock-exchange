"""Tests for the matching engine via API (V2).

Pattern: submit orders via POST /orders -> flush_persistence() -> assert via GET.
V2 orders are matched in-memory; DB writes happen asynchronously via the
persistence loop. flush_persistence() bridges the gap for deterministic tests.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.engine import Order as EngineOrder, engine
from app.engine.persistence import NewOrderItem
from app.models import OrderSide, OrderStatus, OrderType
from tests.conftest import (
    admin_header,
    auth_header,
    flush_persistence,
    make_limit_order,
    make_market_order,
)


@pytest.mark.asyncio
class TestBasicMatching:

    async def test_same_price_match(self, client: AsyncClient, broker_with_key):
        """Sell 1000@1000, Buy 1000@1000 -> both closed, 1 trade@1000."""
        _, key = broker_with_key

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        assert sell.status_code == 201
        sell_id = sell.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        assert buy.status_code == 201
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        sell_data = (await client.get(f"/orders/{sell_id}", headers=auth_header(key))).json()
        assert sell_data["remaining_quantity"] == 0
        assert sell_data["status"] == "closed"
        assert len(sell_data["trades"]) == 1
        assert sell_data["trades"][0]["price"] == 1000
        assert sell_data["trades"][0]["quantity"] == 1000

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 0
        assert buy_data["status"] == "closed"

    async def test_no_match_price_gap(self, client: AsyncClient, broker_with_key):
        """Sell@2000, Buy@1000 -> both open, no trades."""
        _, key = broker_with_key

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=2000, quantity=1000),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        sell_data = (await client.get(f"/orders/{sell_id}", headers=auth_header(key))).json()
        assert sell_data["remaining_quantity"] == 1000
        assert sell_data["status"] == "open"

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 1000
        assert buy_data["status"] == "open"

    async def test_price_gap_uses_seller_price(self, client: AsyncClient, broker_with_key):
        """Sell@1000, Buy@2000 -> trade@1000 (seller's price)."""
        _, key = broker_with_key

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=2000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["trades"][0]["price"] == 1000

    async def test_seller_price_when_sell_is_incoming(self, client: AsyncClient, broker_with_key):
        """Resting Bid@2000, then Ask@1000 -> trade@1000."""
        _, key = broker_with_key

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=2000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        await flush_persistence()

        sell_data = (await client.get(f"/orders/{sell_id}", headers=auth_header(key))).json()
        assert sell_data["trades"][0]["price"] == 1000


@pytest.mark.asyncio
class TestPartialFills:

    async def test_partial_fill_buyer_larger(self, client: AsyncClient, broker_with_key):
        """Sell 500@1000, Buy 1000@1000 -> Sell closed, Buy open remaining=500."""
        _, key = broker_with_key

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        sell_data = (await client.get(f"/orders/{sell_id}", headers=auth_header(key))).json()
        assert sell_data["remaining_quantity"] == 0
        assert sell_data["status"] == "closed"

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 500
        assert buy_data["status"] == "open"

    async def test_multiple_sellers_partial_fill(self, client: AsyncClient, broker_with_key):
        """2x Sell 500@1000, Buy 1500@1000 -> both sells closed, Buy open remaining=500."""
        _, key = broker_with_key

        sell_a = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )
        sell_a_id = sell_a.json()["order_id"]

        sell_b = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )
        sell_b_id = sell_b.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1500),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        a_data = (await client.get(f"/orders/{sell_a_id}", headers=auth_header(key))).json()
        assert a_data["remaining_quantity"] == 0
        assert a_data["status"] == "closed"

        b_data = (await client.get(f"/orders/{sell_b_id}", headers=auth_header(key))).json()
        assert b_data["remaining_quantity"] == 0
        assert b_data["status"] == "closed"

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 500
        assert buy_data["status"] == "open"
        assert len(buy_data["trades"]) == 2


@pytest.mark.asyncio
class TestOrderPriority:

    async def test_fifo_same_price(self, client: AsyncClient, broker_with_key):
        """Sell A 1000@1000, Sell B 1000@1000, Buy 1000@1000 -> A closed, B open."""
        _, key = broker_with_key

        sell_a = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_a_id = sell_a.json()["order_id"]

        sell_b = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_b_id = sell_b.json()["order_id"]

        await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )

        await flush_persistence()

        a_data = (await client.get(f"/orders/{sell_a_id}", headers=auth_header(key))).json()
        assert a_data["remaining_quantity"] == 0
        assert a_data["status"] == "closed"

        b_data = (await client.get(f"/orders/{sell_b_id}", headers=auth_header(key))).json()
        assert b_data["remaining_quantity"] == 1000
        assert b_data["status"] == "open"

    async def test_best_price_wins(self, client: AsyncClient, broker_with_key):
        """Sell A@1200, Sell B@1000, Buy@1500 qty 1000 -> B closed, A open, trade@1000."""
        _, key = broker_with_key

        sell_a = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1200, quantity=1000),
            headers=auth_header(key),
        )
        sell_a_id = sell_a.json()["order_id"]

        sell_b = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_b_id = sell_b.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1500, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        b_data = (await client.get(f"/orders/{sell_b_id}", headers=auth_header(key))).json()
        assert b_data["remaining_quantity"] == 0

        a_data = (await client.get(f"/orders/{sell_a_id}", headers=auth_header(key))).json()
        assert a_data["remaining_quantity"] == 1000

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["trades"][0]["price"] == 1000


@pytest.mark.asyncio
class TestMarketOrders:

    async def test_market_buy_fills_immediately(self, client: AsyncClient, broker_with_key):
        """Ask@1000 qty 500, Market Buy qty 500 -> Buy closed, trade@1000."""
        _, key = broker_with_key

        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )

        buy = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=500),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 0
        assert buy_data["status"] == "closed"
        assert buy_data["trades"][0]["price"] == 1000

    async def test_market_order_ioc_partial(self, client: AsyncClient, broker_with_key):
        """Ask@1000 qty 300, Market Buy qty 1000 -> Buy closed (IOC), remaining=700."""
        _, key = broker_with_key

        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=300),
            headers=auth_header(key),
        )

        buy = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 700
        assert buy_data["status"] == "closed"

    async def test_market_order_no_liquidity(self, client: AsyncClient, broker_with_key):
        """Market Buy on empty book -> closed, remaining=full, 0 trades."""
        _, key = broker_with_key

        buy = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=1000, symbol="ZZZZ9"),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 1000
        assert buy_data["status"] == "closed"
        assert len(buy_data["trades"]) == 0


@pytest.mark.asyncio
class TestPersistenceVisibility:
    """V2-specific: verify the async persistence gap."""

    async def test_order_returns_404_before_flush(self, client: AsyncClient, broker_with_key):
        """Submit order, don't flush -> GET returns 404 (not in DB yet)."""
        _, key = broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        # Don't flush — order is only in memory, not in DB
        get_resp = await client.get(f"/orders/{order_id}", headers=auth_header(key))
        assert get_resp.status_code == 404

    async def test_order_appears_after_flush(self, client: AsyncClient, broker_with_key):
        """Submit order, flush -> GET returns 200 with correct data."""
        _, key = broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1500, quantity=200),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        await flush_persistence()

        get_resp = await client.get(f"/orders/{order_id}", headers=auth_header(key))
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["price"] == 1500
        assert data["quantity"] == 200
        assert data["status"] == "open"

    async def test_balance_updates_after_trade(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        """Two brokers trade, flush -> seller balance positive, buyer negative."""
        _, seller_key = broker_with_key
        _, buyer_key = second_broker_with_key

        # Seller posts ask
        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=100),
            headers=auth_header(seller_key),
        )

        # Buyer posts crossing bid
        await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=100),
            headers=auth_header(buyer_key),
        )

        await flush_persistence()

        # cost = price * quantity = 1000 * 100 = 100_000
        seller_balance = (await client.get("/balance", headers=auth_header(seller_key))).json()
        assert seller_balance["balance"] == 100_000  # received payment

        buyer_balance = (await client.get("/balance", headers=auth_header(buyer_key))).json()
        assert buyer_balance["balance"] == -100_000  # paid for shares

    async def test_expired_order_skipped_during_match(
        self, client: AsyncClient, broker_with_key
    ):
        """Expired order in book is skipped; match happens against valid sell."""
        broker_id_str, key = broker_with_key
        broker_id = uuid.UUID(broker_id_str)
        now = datetime.now(timezone.utc)

        # Insert expired ask directly into engine (API rejects expired valid_until)
        expired_order = EngineOrder(
            id=uuid.uuid4(),
            broker_id=broker_id,
            symbol="PETR4",
            side=OrderSide.ask,
            order_type=OrderType.limit,
            price=1000,
            quantity=1000,
            remaining_quantity=1000,
            status=OrderStatus.open,
            document_number="expired",
            valid_until=now - timedelta(hours=1),
            created_at=now - timedelta(hours=2),
        )
        engine.orders[expired_order.id] = expired_order
        engine.book.insert(expired_order)

        # Queue persistence so DB knows about it
        engine.queue.put_nowait(NewOrderItem(
            id=expired_order.id,
            broker_id=expired_order.broker_id,
            symbol=expired_order.symbol,
            side=expired_order.side,
            order_type=expired_order.order_type,
            price=expired_order.price,
            quantity=expired_order.quantity,
            remaining_quantity=expired_order.remaining_quantity,
            status=expired_order.status,
            document_number=expired_order.document_number,
            valid_until=expired_order.valid_until,
            created_at=expired_order.created_at,
        ))

        # Submit valid sell via API
        valid_sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        valid_sell_id = valid_sell.json()["order_id"]

        # Submit crossing buy — should match valid sell, skip expired
        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        await flush_persistence()

        # Buy matched against valid sell
        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["status"] == "closed"
        assert buy_data["remaining_quantity"] == 0
        assert len(buy_data["trades"]) == 1

        # Valid sell also closed
        sell_data = (await client.get(f"/orders/{valid_sell_id}", headers=auth_header(key))).json()
        assert sell_data["status"] == "closed"

        # Expired order closed with no trades, full remaining
        expired_data = (
            await client.get(f"/orders/{expired_order.id}", headers=auth_header(key))
        ).json()
        assert expired_data["status"] == "closed"
        assert expired_data["remaining_quantity"] == 1000
        assert len(expired_data["trades"]) == 0


@pytest.mark.asyncio
class TestPersistenceLoop:
    """Validate the background persistence task flushes items (Option B: queue.join)."""

    async def test_persistence_loop_flushes_order(
        self, with_persistence_loop, client: AsyncClient, broker_with_key
    ):
        """Submit order, queue.join() -> order visible in DB."""
        _, key = broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        # Wait for the background persistence loop to flush (not manual flush)
        await asyncio.wait_for(engine.queue.join(), timeout=5.0)

        get_resp = await client.get(f"/orders/{order_id}", headers=auth_header(key))
        assert get_resp.status_code == 200

    async def test_persistence_loop_flushes_trade(
        self, with_persistence_loop, client: AsyncClient, broker_with_key
    ):
        """Submit crossing orders, queue.join() -> trade visible in DB."""
        _, key = broker_with_key

        # Resting ask
        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )

        # Crossing bid
        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=500),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        # Wait for persistence loop
        await asyncio.wait_for(engine.queue.join(), timeout=5.0)

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["status"] == "closed"
        assert len(buy_data["trades"]) == 1


@pytest.mark.asyncio
class TestOrderCancel:

    async def test_cancel_open_order(self, client: AsyncClient, broker_with_key):
        """Cancel an open limit order -> 204, verify state via GET."""
        _, key = broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        await flush_persistence()

        cancel_resp = await client.post(
            f"/orders/{order_id}/cancel", headers=auth_header(key)
        )
        assert cancel_resp.status_code == 204

        # Verify via GET after flush
        await flush_persistence()
        get_data = (await client.get(f"/orders/{order_id}", headers=auth_header(key))).json()
        assert get_data["status"] == "closed"
        assert get_data["remaining_quantity"] == 1000

    async def test_cancel_nonexistent_order(self, client: AsyncClient, broker_with_key):
        """Cancel a non-existent order -> 204 (silent no-op)."""
        _, key = broker_with_key
        fake_id = uuid.uuid4()

        resp = await client.post(
            f"/orders/{fake_id}/cancel", headers=auth_header(key)
        )
        assert resp.status_code == 204

    async def test_cancel_other_brokers_order(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        """Broker2 tries to cancel Broker1's order -> 403."""
        _, key1 = broker_with_key
        _, key2 = second_broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key1),
        )
        order_id = resp.json()["order_id"]

        cancel_resp = await client.post(
            f"/orders/{order_id}/cancel", headers=auth_header(key2)
        )
        assert cancel_resp.status_code == 403

    async def test_cancel_already_closed_order(self, client: AsyncClient, broker_with_key):
        """Cancel the same order twice -> both return 204."""
        _, key = broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        await flush_persistence()

        cancel1 = await client.post(
            f"/orders/{order_id}/cancel", headers=auth_header(key)
        )
        assert cancel1.status_code == 204

        await flush_persistence()

        cancel2 = await client.post(
            f"/orders/{order_id}/cancel", headers=auth_header(key)
        )
        assert cancel2.status_code == 204

        # Verify via GET
        get_data = (await client.get(f"/orders/{order_id}", headers=auth_header(key))).json()
        assert get_data["status"] == "closed"

    async def test_cancel_market_order(self, client: AsyncClient, broker_with_key):
        """Cancel a market order -> 204 (already closed via IOC, silent no-op)."""
        _, key = broker_with_key

        # Create liquidity so market order can be submitted
        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=100),
            headers=auth_header(key),
        )

        resp = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=100),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        await flush_persistence()

        cancel_resp = await client.post(
            f"/orders/{order_id}/cancel", headers=auth_header(key)
        )
        assert cancel_resp.status_code == 204

    async def test_cancel_partially_filled_order(self, client: AsyncClient, broker_with_key):
        """Ask 100@1000, Bid 40@1000 -> partial fill, cancel ask -> 204, verify via GET."""
        _, key = broker_with_key

        ask = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=100),
            headers=auth_header(key),
        )
        ask_id = ask.json()["order_id"]

        await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=40),
            headers=auth_header(key),
        )

        await flush_persistence()

        cancel_resp = await client.post(
            f"/orders/{ask_id}/cancel", headers=auth_header(key)
        )
        assert cancel_resp.status_code == 204

        await flush_persistence()
        get_data = (await client.get(f"/orders/{ask_id}", headers=auth_header(key))).json()
        assert get_data["status"] == "closed"
        assert get_data["remaining_quantity"] == 60
        assert len(get_data["trades"]) == 1
        assert get_data["trades"][0]["quantity"] == 40

    async def test_cancelled_order_not_matched(self, client: AsyncClient, broker_with_key):
        """Create ask, cancel it, create crossing bid -> bid stays open, no match."""
        _, key = broker_with_key

        ask = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        ask_id = ask.json()["order_id"]

        await flush_persistence()

        cancel_resp = await client.post(
            f"/orders/{ask_id}/cancel", headers=auth_header(key)
        )
        assert cancel_resp.status_code == 204

        # Now submit a crossing bid — should NOT match the cancelled ask
        bid = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        bid_id = bid.json()["order_id"]

        await flush_persistence()

        bid_data = (await client.get(f"/orders/{bid_id}", headers=auth_header(key))).json()
        assert bid_data["status"] == "open"
        assert bid_data["remaining_quantity"] == 1000
        assert len(bid_data["trades"]) == 0

    async def test_cancel_no_auth(self, client: AsyncClient, broker_with_key):
        """Cancel without auth header -> 403."""
        _, key = broker_with_key

        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        cancel_resp = await client.post(f"/orders/{order_id}/cancel")
        assert cancel_resp.status_code == 403
