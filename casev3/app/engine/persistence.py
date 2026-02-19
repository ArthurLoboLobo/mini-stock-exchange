from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.models import OrderSide, OrderStatus, OrderType
from app.schemas import WebhookPayload
from app.services.webhooks import fire_webhooks

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.engine import Engine

logger = logging.getLogger(__name__)


# --- Queue item dataclasses (value snapshots, not references) ---


@dataclass(frozen=True)
class NewOrderItem:
    """Pristine snapshot of an order at creation time."""
    id: uuid.UUID
    broker_id: uuid.UUID
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: int | None
    quantity: int
    remaining_quantity: int
    status: OrderStatus
    document_number: str
    valid_until: datetime
    created_at: datetime


@dataclass(frozen=True)
class TradeItem:
    """Single trade with broker context for persistence."""
    id: uuid.UUID
    buy_order_id: uuid.UUID
    sell_order_id: uuid.UUID
    symbol: str
    price: int
    quantity: int
    buyer_broker_id: uuid.UUID
    seller_broker_id: uuid.UUID
    buyer_remaining_qty: int
    seller_remaining_qty: int
    created_at: datetime


@dataclass(frozen=True)
class OrderUpdateItem:
    """Value snapshot of an order status/quantity change."""
    order_id: uuid.UUID
    status: OrderStatus
    remaining_quantity: int


# --- Flush logic ---


async def flush_batch(
    items: list,
    session_factory,
    engine: Engine,
) -> None:
    """Group items by kind, deduplicate, and flush to DB in one transaction."""
    new_orders: list[NewOrderItem] = []
    trades: list[TradeItem] = []
    order_updates: dict[uuid.UUID, OrderUpdateItem] = {}  # dedup by order_id, keep last

    for item in items:
        if isinstance(item, NewOrderItem):
            new_orders.append(item)
        elif isinstance(item, TradeItem):
            trades.append(item)
        elif isinstance(item, OrderUpdateItem):
            order_updates[item.order_id] = item  # last wins

    if not new_orders and not trades and not order_updates:
        return

    # Collect broker IDs involved in trades (for webhook query after commit)
    trade_broker_ids: set[uuid.UUID] = set()
    for t in trades:
        trade_broker_ids.add(t.buyer_broker_id)
        trade_broker_ids.add(t.seller_broker_id)

    async with session_factory() as session:
        async with session.begin():
            # 1. INSERT orders
            if new_orders:
                params = [
                    {
                        "id": str(o.id),
                        "broker_id": str(o.broker_id),
                        "symbol": o.symbol,
                        "side": o.side.value,
                        "order_type": o.order_type.value,
                        "price": o.price,
                        "quantity": o.quantity,
                        "remaining_quantity": o.remaining_quantity,
                        "status": o.status.value,
                        "document_number": o.document_number,
                        "valid_until": o.valid_until,
                        "created_at": o.created_at,
                    }
                    for o in new_orders
                ]
                await session.execute(
                    text(
                        "INSERT INTO orders (id, broker_id, symbol, side, order_type, price, "
                        "quantity, remaining_quantity, status, document_number, valid_until, created_at) "
                        "VALUES (:id, :broker_id, :symbol, CAST(:side AS order_side), CAST(:order_type AS order_type), "
                        ":price, :quantity, :remaining_quantity, CAST(:status AS order_status), "
                        ":document_number, :valid_until, :created_at)"
                    ),
                    params,
                )

            # 2. INSERT trades
            if trades:
                params = [
                    {
                        "id": str(t.id),
                        "buy_order_id": str(t.buy_order_id),
                        "sell_order_id": str(t.sell_order_id),
                        "symbol": t.symbol,
                        "price": t.price,
                        "quantity": t.quantity,
                        "created_at": t.created_at,
                    }
                    for t in trades
                ]
                await session.execute(
                    text(
                        "INSERT INTO trades (id, buy_order_id, sell_order_id, symbol, price, "
                        "quantity, created_at) "
                        "VALUES (:id, :buy_order_id, :sell_order_id, :symbol, :price, "
                        ":quantity, :created_at)"
                    ),
                    params,
                )

            # 3. UPDATE orders (deduplicated)
            if order_updates:
                params = [
                    {
                        "id": str(u.order_id),
                        "status": u.status.value,
                        "remaining_quantity": u.remaining_quantity,
                    }
                    for u in order_updates.values()
                ]
                await session.execute(
                    text(
                        "UPDATE orders SET status = CAST(:status AS order_status), "
                        "remaining_quantity = :remaining_quantity "
                        "WHERE id = :id"
                    ),
                    params,
                )

            # 4. UPDATE brokers.balance
            if trades:
                balance_deltas: dict[uuid.UUID, int] = {}
                for t in trades:
                    cost = t.price * t.quantity
                    balance_deltas[t.buyer_broker_id] = balance_deltas.get(t.buyer_broker_id, 0) - cost
                    balance_deltas[t.seller_broker_id] = balance_deltas.get(t.seller_broker_id, 0) + cost

                for broker_id, delta in balance_deltas.items():
                    await session.execute(
                        text("UPDATE brokers SET balance = balance + :delta WHERE id = :id"),
                        {"delta": delta, "id": str(broker_id)},
                    )

        # --- After commit ---

        # Fire webhooks for committed trades
        if trades and trade_broker_ids:
            broker_urls: dict[str, str | None] = {}
            for bid in trade_broker_ids:
                info = engine.brokers.get(bid)
                if info is not None:
                    broker_urls[str(bid)] = info.webhook_url

            webhooks: list[tuple[str, WebhookPayload]] = []
            for t in trades:
                # Webhook to buyer's broker
                buyer_url = broker_urls.get(str(t.buyer_broker_id))
                if buyer_url:
                    webhooks.append((
                        buyer_url,
                        WebhookPayload(
                            trade_id=t.id,
                            order_id=t.buy_order_id,
                            symbol=t.symbol,
                            side=OrderSide.bid,
                            price=t.price,
                            quantity=t.quantity,
                            order_remaining_quantity=t.buyer_remaining_qty,
                            executed_at=t.created_at,
                        ),
                    ))
                # Webhook to seller's broker
                seller_url = broker_urls.get(str(t.seller_broker_id))
                if seller_url:
                    webhooks.append((
                        seller_url,
                        WebhookPayload(
                            trade_id=t.id,
                            order_id=t.sell_order_id,
                            symbol=t.symbol,
                            side=OrderSide.ask,
                            price=t.price,
                            quantity=t.quantity,
                            order_remaining_quantity=t.seller_remaining_qty,
                            executed_at=t.created_at,
                        ),
                    ))

            if webhooks:
                fire_webhooks(webhooks)


# --- Background persistence loop ---


FLUSH_INTERVAL_MS = 30


async def run_persistence_loop(engine: Engine, session_factory) -> None:
    """Background task: drain queue and flush to DB in batches."""
    try:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_MS / 1000)

            items: list = []
            while not engine.queue.empty():
                try:
                    items.append(engine.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if not items:
                continue

            try:
                await flush_batch(items, session_factory, engine)
            except Exception:
                logger.exception("Persistence flush failed for batch of %d items", len(items))
            finally:
                for _ in items:
                    engine.queue.task_done()

    except asyncio.CancelledError:
        # Graceful shutdown: flush remaining items
        items = []
        while not engine.queue.empty():
            try:
                items.append(engine.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if items:
            try:
                await flush_batch(items, session_factory, engine)
            except Exception:
                logger.exception("Persistence flush failed during shutdown")
            finally:
                for _ in items:
                    engine.queue.task_done()
        raise
