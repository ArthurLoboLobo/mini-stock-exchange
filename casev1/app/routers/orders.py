import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth import get_current_broker
from app.database import get_db
from app.models import Broker, Order, OrderSide, OrderStatus, OrderType, Trade
from app.schemas import OrderCreate, OrderCreated, OrderDetail, TradeInfo, WebhookPayload
from app.services.matching import match_order
from app.services.webhooks import fire_webhooks

router = APIRouter()


@router.post("/orders", status_code=201, response_model=OrderCreated)
async def create_order(
    body: OrderCreate,
    broker: Broker = Depends(get_current_broker),
    db: AsyncSession = Depends(get_db),
):
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
    order = Order(
        broker_id=broker.id,
        document_number=body.document_number,
        side=body.side,
        order_type=body.order_type,
        symbol=body.symbol.upper(),
        price=body.price,
        quantity=body.quantity,
        remaining_quantity=body.quantity,
        valid_until=body.valid_until if body.order_type == OrderType.limit else now,
        status=OrderStatus.open,
    )

    db.add(order)
    await db.flush()

    trades = await match_order(order, db)

    # Market orders are always closed after matching (IOC behavior)
    if order.order_type == OrderType.market and order.status != OrderStatus.closed:
        order.status = OrderStatus.closed

    await db.commit()

    # Fire webhooks after commit (non-blocking)
    if trades:
        # Collect matched order IDs and batch-load them with their brokers
        matched_order_ids = []
        for trade in trades:
            if trade.buy_order_id == order.id:
                matched_order_ids.append(trade.sell_order_id)
            else:
                matched_order_ids.append(trade.buy_order_id)

        # Single batch query: load all matched orders + their brokers
        matched_alias = aliased(Order)
        broker_alias = aliased(Broker)
        batch_result = await db.execute(
            select(matched_alias, broker_alias)
            .join(broker_alias, matched_alias.broker_id == broker_alias.id)
            .where(matched_alias.id.in_(matched_order_ids))
        )
        matched_map = {row[0].id: (row[0], row[1]) for row in batch_result.all()}

        webhook_payloads: list[tuple[str, WebhookPayload]] = []
        for trade, matched_order_id in zip(trades, matched_order_ids):
            matched_order, matched_broker = matched_map[matched_order_id]
            now_ts = trade.created_at or datetime.now(timezone.utc)

            # Webhook for incoming order's broker
            if broker.webhook_url:
                webhook_payloads.append((
                    broker.webhook_url,
                    WebhookPayload(
                        trade_id=trade.id,
                        order_id=order.id,
                        symbol=trade.symbol,
                        side=order.side,
                        price=trade.price,
                        quantity=trade.quantity,
                        order_remaining_quantity=order.remaining_quantity,
                        executed_at=now_ts,
                    ),
                ))

            # Webhook for matched order's broker
            if matched_broker.webhook_url:
                webhook_payloads.append((
                    matched_broker.webhook_url,
                    WebhookPayload(
                        trade_id=trade.id,
                        order_id=matched_order.id,
                        symbol=trade.symbol,
                        side=matched_order.side,
                        price=trade.price,
                        quantity=trade.quantity,
                        order_remaining_quantity=matched_order.remaining_quantity,
                        executed_at=now_ts,
                    ),
                ))

        if webhook_payloads:
            fire_webhooks(webhook_payloads)

    return OrderCreated(order_id=order.id)


@router.post("/orders/{order_id}/cancel", status_code=204)
async def cancel_order(
    order_id: uuid.UUID,
    broker: Broker = Depends(get_current_broker),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    order = result.scalar_one_or_none()

    if order is None:
        # Silent no-op for consistency with casev2
        return Response(status_code=204)

    if order.broker_id != broker.id:
        raise HTTPException(status_code=403, detail="Order belongs to a different broker")

    if order.status != OrderStatus.open:
        # Already closed — no-op
        return Response(status_code=204)

    order.status = OrderStatus.closed
    await db.commit()

    return Response(status_code=204)


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: uuid.UUID,
    broker: Broker = Depends(get_current_broker),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.broker_id != broker.id:
        raise HTTPException(status_code=403, detail="Order belongs to a different broker")

    # Lazy-close expired orders on read
    now = datetime.now(timezone.utc)
    if order.status == OrderStatus.open and order.valid_until < now:
        order.status = OrderStatus.closed
        await db.commit()
        await db.refresh(order)

    # Load trades with counterparty info in a single joined query
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
