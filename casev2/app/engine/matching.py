from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.engine import Order, Trade
from app.engine.order_book import OrderBook
from app.models import OrderSide, OrderType, OrderStatus


def match_order(order: Order, book: OrderBook) -> tuple[list[Trade], list[Order]]:
    """Match an incoming order against the book. Pure Python, zero I/O.

    Returns (trades, expired_orders). Mutates orders in place.
    The caller is responsible for queuing persistence items.
    """
    trades: list[Trade] = []
    expired_orders: list[Order] = []

    if order.side == OrderSide.bid:
        _match_bid(order, book, trades, expired_orders)
    else:
        _match_ask(order, book, trades, expired_orders)

    # Post-matching: handle remaining quantity
    if order.remaining_quantity > 0:
        if order.order_type == OrderType.market:
            # IOC: cancel unfilled remainder
            order.status = OrderStatus.closed
        else:
            # Limit order: rest in the book
            book.insert(order)
    else:
        order.status = OrderStatus.closed

    return trades, expired_orders


def _match_bid(
    order: Order,
    book: OrderBook,
    trades: list[Trade],
    expired_orders: list[Order],
) -> None:
    """Match a buy order against asks (lowest first)."""
    now = datetime.now(timezone.utc)
    is_market = order.order_type == OrderType.market

    while order.remaining_quantity > 0:
        best = book.get_best_ask(order.symbol)
        if best is None:
            break

        best_price, best_deque = best

        # Price check (skip for market orders)
        if not is_market and best_price > order.price:
            break

        # Peek at the front counterparty
        counterparty = best_deque[0]

        # Lazy expiration check
        if counterparty.valid_until < now:
            counterparty.status = OrderStatus.closed
            expired_orders.append(counterparty)
            book.remove_front(order.symbol, OrderSide.ask, best_price)
            continue

        # Execute trade
        trade_qty = min(order.remaining_quantity, counterparty.remaining_quantity)
        # Execution price = seller's (counterparty's) price
        trade_price = counterparty.price

        trade = Trade(
            id=uuid.uuid4(),
            buy_order_id=order.id,
            sell_order_id=counterparty.id,
            symbol=order.symbol,
            price=trade_price,
            quantity=trade_qty,
            buyer_broker_id=order.broker_id,
            seller_broker_id=counterparty.broker_id,
            created_at=now,
        )
        trades.append(trade)

        # Update quantities
        order.remaining_quantity -= trade_qty
        counterparty.remaining_quantity -= trade_qty

        # Remove fully filled counterparty from book
        if counterparty.remaining_quantity == 0:
            counterparty.status = OrderStatus.closed
            book.remove_front(order.symbol, OrderSide.ask, best_price)


def _match_ask(
    order: Order,
    book: OrderBook,
    trades: list[Trade],
    expired_orders: list[Order],
) -> None:
    """Match a sell order against bids (highest first)."""
    now = datetime.now(timezone.utc)
    is_market = order.order_type == OrderType.market

    while order.remaining_quantity > 0:
        best = book.get_best_bid(order.symbol)
        if best is None:
            break

        best_price, best_deque = best

        # Price check (skip for market orders)
        if not is_market and best_price < order.price:
            break

        # Peek at the front counterparty
        counterparty = best_deque[0]

        # Lazy expiration check
        if counterparty.valid_until < now:
            counterparty.status = OrderStatus.closed
            expired_orders.append(counterparty)
            book.remove_front(order.symbol, OrderSide.bid, best_price)
            continue

        # Execute trade — execution price is always the seller's price
        trade_qty = min(order.remaining_quantity, counterparty.remaining_quantity)
        if order.price is not None:
            trade_price = order.price  # incoming seller's limit price
        else:
            trade_price = counterparty.price  # market sell → use buyer's price

        trade = Trade(
            id=uuid.uuid4(),
            buy_order_id=counterparty.id,
            sell_order_id=order.id,
            symbol=order.symbol,
            price=trade_price,
            quantity=trade_qty,
            buyer_broker_id=counterparty.broker_id,
            seller_broker_id=order.broker_id,
            created_at=now,
        )
        trades.append(trade)

        # Update quantities
        order.remaining_quantity -= trade_qty
        counterparty.remaining_quantity -= trade_qty

        # Remove fully filled counterparty from book
        if counterparty.remaining_quantity == 0:
            counterparty.status = OrderStatus.closed
            book.remove_front(order.symbol, OrderSide.bid, best_price)
