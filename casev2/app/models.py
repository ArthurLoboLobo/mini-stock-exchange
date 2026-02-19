import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    ForeignKey,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderSide(str, enum.Enum):
    bid = "bid"
    ask = "ask"


class OrderType(str, enum.Enum):
    limit = "limit"
    market = "market"


class OrderStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class Broker(Base):
    __tablename__ = "brokers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="broker")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("brokers.id"), nullable=False)
    document_number: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide, name="order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType, name="order_type"), nullable=False, default=OrderType.limit)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="order_status"), nullable=False, default=OrderStatus.open)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    broker: Mapped["Broker"] = relationship(back_populates="orders")
    buy_trades: Mapped[list["Trade"]] = relationship(foreign_keys="Trade.buy_order_id", back_populates="buy_order")
    sell_trades: Mapped[list["Trade"]] = relationship(foreign_keys="Trade.sell_order_id", back_populates="sell_order")

    __table_args__ = (
        Index(
            "ix_orders_matching",
            "symbol", "side", "price", "created_at",
            postgresql_where=(status == OrderStatus.open),
        ),
        Index("ix_orders_broker", "broker_id", "created_at"),
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buy_order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    sell_order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    buy_order: Mapped["Order"] = relationship(foreign_keys=[buy_order_id], back_populates="buy_trades")
    sell_order: Mapped["Order"] = relationship(foreign_keys=[sell_order_id], back_populates="sell_trades")

    __table_args__ = (
        Index("ix_trades_symbol", "symbol", "created_at"),
        Index("ix_trades_buy_order", "buy_order_id"),
        Index("ix_trades_sell_order", "sell_order_id"),
    )
