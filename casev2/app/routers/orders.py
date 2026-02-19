import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth import get_current_broker_id
from app.database import get_db
from app.engine import Order as EngineOrder, engine
from app.engine.matching import match_order
from app.engine.persistence import NewOrderItem, OrderUpdateItem, TradeItem
from app.models import Broker, Order, OrderSide, OrderStatus, OrderType, Trade
from app.schemas import OrderCreate, OrderCreated, OrderDetail, TradeInfo

router = APIRouter()


@router.post("/orders", status_code=201, response_model=OrderCreated)
async def create_order(
    body: OrderCreate,
    broker_id: uuid.UUID = Depends(get_current_broker_id),
):
    # --- Validate ---
    if body.order_type == OrderType.limit:
        if body.price is None:
            raise HTTPException(status_code=422, detail="Limit orders require a price")
        if body.valid_until is None:
            raise HTTPException(status_code=422, detail="Limit orders require valid_until")
        if body.valid_until <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="valid_until must be in the future")
    elif body.order_type == OrderType.market:
        if body.price is not None:
            raise HTTPException(status_code=422, detail="Market orders must not have a price")

    now = datetime.now(timezone.utc)

    # --- Create in-memory Order ---
    order = EngineOrder(
        id=uuid.uuid4(),
        broker_id=broker_id,
        symbol=body.symbol.upper(),
        side=body.side,
        order_type=body.order_type,
        price=body.price,
        quantity=body.quantity,
        remaining_quantity=body.quantity,
        status=OrderStatus.open,
        document_number=body.document_number,
        valid_until=body.valid_until if body.order_type == OrderType.limit else now,
        created_at=now,
    )

    # --- Add to engine orders dict ---
    engine.orders[order.id] = order

    # --- Queue pristine snapshot BEFORE matching ---
    engine.queue.put_nowait(NewOrderItem(
        id=order.id,
        broker_id=order.broker_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        price=order.price,
        quantity=order.quantity,
        remaining_quantity=order.remaining_quantity,
        status=order.status,
        document_number=order.document_number,
        valid_until=order.valid_until,
        created_at=order.created_at,
    ))

    # --- Match ---
    trades, expired_orders = match_order(order, engine.book)

    # --- Queue persistence items ---

    # TradeItems
    for t in trades:
        engine.queue.put_nowait(TradeItem(
            id=t.id,
            buy_order_id=t.buy_order_id,
            sell_order_id=t.sell_order_id,
            symbol=t.symbol,
            price=t.price,
            quantity=t.quantity,
            buyer_broker_id=t.buyer_broker_id,
            seller_broker_id=t.seller_broker_id,
            buyer_remaining_qty=engine.orders[t.buy_order_id].remaining_quantity
            if t.buy_order_id in engine.orders else 0,
            seller_remaining_qty=engine.orders[t.sell_order_id].remaining_quantity
            if t.sell_order_id in engine.orders else 0,
            created_at=t.created_at,
        ))

    # OrderUpdateItems for all modified orders (incoming + counterparties)
    # Collect unique modified order IDs
    modified_order_ids: set[uuid.UUID] = set()

    # The incoming order is always modified (either filled, partially filled, or resting)
    modified_order_ids.add(order.id)

    # Counterparties from trades
    for t in trades:
        modified_order_ids.add(t.buy_order_id)
        modified_order_ids.add(t.sell_order_id)

    for oid in modified_order_ids:
        o = engine.orders.get(oid)
        if o is not None:
            engine.queue.put_nowait(OrderUpdateItem(
                order_id=o.id,
                status=o.status,
                remaining_quantity=o.remaining_quantity,
            ))

    # Expired orders discovered during matching
    for exp in expired_orders:
        engine.queue.put_nowait(OrderUpdateItem(
            order_id=exp.id,
            status=exp.status,
            remaining_quantity=exp.remaining_quantity,
        ))

    return OrderCreated(order_id=order.id)


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: uuid.UUID,
    broker_id: uuid.UUID = Depends(get_current_broker_id),
    db: AsyncSession = Depends(get_db),
):
    # DB lookup (single source of truth for reads)
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.broker_id != broker_id:
        raise HTTPException(status_code=403, detail="Order belongs to a different broker")

    # Lazy expiration check
    now = datetime.now(timezone.utc)
    if order.status == OrderStatus.open and order.valid_until < now:
        # In-memory cleanup (if order is still in memory)
        mem_order = engine.orders.get(order_id)
        if mem_order is not None:
            mem_order.status = OrderStatus.closed
            engine.book.remove_order(mem_order)

        # Queue persistence update
        engine.queue.put_nowait(OrderUpdateItem(
            order_id=order_id,
            status=OrderStatus.closed,
            remaining_quantity=order.remaining_quantity,
        ))

        # Show as closed in response
        order.status = OrderStatus.closed

    # Load trades with counterparty info
    counter_order = aliased(Order)
    counter_broker = aliased(Broker)

    trades_query = (
        select(Trade, counter_order, counter_broker)
        .join(
            counter_order,
            (
                (Trade.buy_order_id == order.id) & (counter_order.id == Trade.sell_order_id)
                | (Trade.sell_order_id == order.id) & (counter_order.id == Trade.buy_order_id)
            ),
        )
        .join(counter_broker, counter_broker.id == counter_order.broker_id)
        .where(
            (Trade.buy_order_id == order.id) | (Trade.sell_order_id == order.id)
        )
        .order_by(Trade.created_at)
    )
    trades_result = await db.execute(trades_query)

    trade_infos = []
    for trade, _counter_order, _counter_broker in trades_result.all():
        trade_infos.append(TradeInfo(
            trade_id=trade.id,
            price=trade.price,
            quantity=trade.quantity,
            counterparty_broker=_counter_broker.name,
            executed_at=trade.created_at,
        ))

    return OrderDetail(
        id=order.id,
        side=order.side,
        order_type=order.order_type,
        symbol=order.symbol,
        price=order.price,
        quantity=order.quantity,
        remaining_quantity=order.remaining_quantity,
        status=order.status,
        valid_until=order.valid_until,
        created_at=order.created_at,
        trades=trade_infos,
    )


async def _load_trades(db: AsyncSession, order_id: uuid.UUID) -> list[TradeInfo]:
    """Load trades for an order with counterparty info from DB."""
    counter_order = aliased(Order)
    counter_broker = aliased(Broker)

    trades_query = (
        select(Trade, counter_order, counter_broker)
        .join(
            counter_order,
            (
                (Trade.buy_order_id == order_id) & (counter_order.id == Trade.sell_order_id)
                | (Trade.sell_order_id == order_id) & (counter_order.id == Trade.buy_order_id)
            ),
        )
        .join(counter_broker, counter_broker.id == counter_order.broker_id)
        .where(
            (Trade.buy_order_id == order_id) | (Trade.sell_order_id == order_id)
        )
        .order_by(Trade.created_at)
    )
    trades_result = await db.execute(trades_query)

    return [
        TradeInfo(
            trade_id=trade.id,
            price=trade.price,
            quantity=trade.quantity,
            counterparty_broker=_counter_broker.name,
            executed_at=trade.created_at,
        )
        for trade, _counter_order, _counter_broker in trades_result.all()
    ]


@router.post("/orders/{order_id}/cancel", status_code=204)
async def cancel_order(
    order_id: uuid.UUID,
    broker_id: uuid.UUID = Depends(get_current_broker_id),
):
    mem_order = engine.orders.get(order_id)

    if mem_order is None:
        # Not in memory — silent no-op (already closed, expired, or unknown)
        return Response(status_code=204)

    # Validate broker ownership
    if mem_order.broker_id != broker_id:
        raise HTTPException(status_code=403, detail="Order belongs to a different broker")

    if mem_order.status != OrderStatus.open:
        # Already closed — no-op
        return Response(status_code=204)

    # Cancel in memory
    mem_order.status = OrderStatus.closed
    engine.book.remove_order(mem_order)

    # Enqueue async persistence
    engine.queue.put_nowait(OrderUpdateItem(
        order_id=order_id,
        status=OrderStatus.closed,
        remaining_quantity=mem_order.remaining_quantity,
    ))

    return Response(status_code=204)
