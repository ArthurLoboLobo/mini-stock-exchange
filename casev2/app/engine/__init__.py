from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.models import OrderSide, OrderType, OrderStatus
from app.engine.order_book import OrderBook


@dataclass
class Order:
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


@dataclass
class Trade:
    id: uuid.UUID
    buy_order_id: uuid.UUID
    sell_order_id: uuid.UUID
    symbol: str
    price: int
    quantity: int
    buyer_broker_id: uuid.UUID
    seller_broker_id: uuid.UUID
    created_at: datetime


class Engine:
    """Singleton holding all in-memory state for the exchange."""

    def __init__(self) -> None:
        self.orders: dict[uuid.UUID, Order] = {}
        self.book: OrderBook = OrderBook()
        self.brokers_by_key_hash: dict[str, uuid.UUID] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.persistence_task: asyncio.Task | None = None

    def clear(self) -> None:
        """Clear all in-memory state. Used by debug/reset."""
        self.orders.clear()
        self.book.clear()
        self.brokers_by_key_hash.clear()
        # Drain the queue
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break


engine = Engine()
