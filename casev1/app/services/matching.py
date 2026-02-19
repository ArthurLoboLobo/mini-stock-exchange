import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderSide, OrderStatus, OrderType, Trade


async def match_order(order: Order, db: AsyncSession) -> list[Trade]:
    """Match an incoming order against the book. Returns list of executed trades.

    Must be called within a transaction. The caller is responsible for committing.
    """
    trades: list[Trade] = []

    while order.remaining_quantity > 0:
        candidate = await _find_best_match(order, db)
        if candidate is None:
            break

        trade = _execute_trade(order, candidate)
        trades.append(trade)
        db.add(trade)

    return trades


async def _find_best_match(order: Order, db: AsyncSession) -> Order | None:
    """Find the best matching counter-order.

    For a BUY order:  find the cheapest ASK with price <= buyer's price (or any price for market).
    For a SELL order: find the most expensive BID with price >= seller's price (or any price for market).

    Within the same price level, the earliest order wins (FIFO).
    """
    now = datetime.now(timezone.utc)

    query = (
        select(Order)
        .where(
            Order.symbol == order.symbol,
            Order.status == OrderStatus.open,
            Order.remaining_quantity > 0,
            Order.valid_until > now,
        )
        .with_for_update()
        .limit(1)
    )

    if order.side == OrderSide.bid:
        # Buyer wants to buy — match against sellers (asks)
        query = query.where(Order.side == OrderSide.ask)

        if order.order_type == OrderType.limit:
            # Only match asks at or below buyer's max price
            query = query.where(Order.price <= order.price)

        # Best ask = lowest price first, then earliest
        query = query.order_by(Order.price.asc(), Order.created_at.asc())
    else:
        # Seller wants to sell — match against buyers (bids)
        query = query.where(Order.side == OrderSide.bid)

        if order.order_type == OrderType.limit:
            # Only match bids at or above seller's min price
            query = query.where(Order.price >= order.price)

        # Best bid = highest price first, then earliest
        query = query.order_by(Order.price.desc(), Order.created_at.asc())

    result = await db.execute(query)
    return result.scalar_one_or_none()


def _execute_trade(incoming: Order, matched: Order) -> Trade:
    """Execute a trade between two orders. Mutates both orders in place."""
    trade_qty = min(incoming.remaining_quantity, matched.remaining_quantity)

    # Per the case spec: execution price is always the SELLER's price.
    # Identify which order is the sell side to get the correct price.
    if incoming.side == OrderSide.ask:
        # Incoming is the seller
        execution_price = incoming.price if incoming.price is not None else matched.price
    else:
        # Matched is the seller
        execution_price = matched.price

    incoming.remaining_quantity -= trade_qty
    matched.remaining_quantity -= trade_qty

    if matched.remaining_quantity == 0:
        matched.status = OrderStatus.closed
    if incoming.remaining_quantity == 0:
        incoming.status = OrderStatus.closed

    # Determine which is buy and which is sell
    if incoming.side == OrderSide.bid:
        buy_order_id = incoming.id
        sell_order_id = matched.id
    else:
        buy_order_id = matched.id
        sell_order_id = incoming.id

    return Trade(
        id=uuid.uuid4(),
        buy_order_id=buy_order_id,
        sell_order_id=sell_order_id,
        symbol=incoming.symbol,
        price=execution_price,
        quantity=trade_qty,
    )
