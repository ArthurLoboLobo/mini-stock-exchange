from __future__ import annotations

from collections import deque

from sortedcontainers import SortedDict

from app.models import OrderSide

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.engine import Order


class OrderBook:
    """In-memory order book: per-symbol SortedDicts for asks and bids."""

    def __init__(self) -> None:
        self.asks: dict[str, SortedDict] = {}
        self.bids: dict[str, SortedDict] = {}

    def insert(self, order: Order) -> None:
        """Add order to the correct side/symbol/price deque."""
        if order.side == OrderSide.ask:
            book = self.asks
        else:
            book = self.bids

        if order.symbol not in book:
            book[order.symbol] = SortedDict()

        symbol_book = book[order.symbol]
        if order.price not in symbol_book:
            symbol_book[order.price] = deque()

        symbol_book[order.price].append(order)

    def remove_front(self, symbol: str, side: OrderSide, price: int) -> None:
        """Popleft from deque at the given price level, delete level if empty."""
        if side == OrderSide.ask:
            book = self.asks
        else:
            book = self.bids

        symbol_book = book[symbol]
        dq = symbol_book[price]
        dq.popleft()
        if not dq:
            del symbol_book[price]

    def get_best_ask(self, symbol: str) -> tuple[int, deque] | None:
        """Return (price, deque) for the lowest ask, or None if empty."""
        symbol_book = self.asks.get(symbol)
        if not symbol_book:
            return None
        price, dq = symbol_book.peekitem(0)
        return (price, dq)

    def get_best_bid(self, symbol: str) -> tuple[int, deque] | None:
        """Return (price, deque) for the highest bid, or None if empty."""
        symbol_book = self.bids.get(symbol)
        if not symbol_book:
            return None
        price, dq = symbol_book.peekitem(-1)
        return (price, dq)

    def remove_order(self, order: Order) -> None:
        """Remove a specific order from the book (used for lazy expiration on reads)."""
        book = self.asks if order.side == OrderSide.ask else self.bids
        symbol_book = book.get(order.symbol)
        if symbol_book is None or order.price not in symbol_book:
            return
        dq = symbol_book[order.price]
        try:
            dq.remove(order)
        except ValueError:
            return
        if not dq:
            del symbol_book[order.price]

    def clear(self) -> None:
        """Reset all state."""
        self.asks.clear()
        self.bids.clear()
