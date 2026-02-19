import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import OrderSide, OrderType, OrderStatus


class OrderCreate(BaseModel):
    document_number: str = Field(..., min_length=1, max_length=20)
    side: OrderSide
    order_type: OrderType = OrderType.limit
    symbol: str = Field(..., min_length=1, max_length=10)
    price: int | None = Field(None, gt=0)
    quantity: int = Field(..., gt=0)
    valid_until: datetime | None = None


class OrderCreated(BaseModel):
    order_id: uuid.UUID


class TradeInfo(BaseModel):
    trade_id: uuid.UUID
    price: int
    quantity: int
    counterparty_broker: str
    executed_at: datetime

    model_config = {"from_attributes": True}


class OrderDetail(BaseModel):
    id: uuid.UUID
    side: OrderSide
    order_type: OrderType
    symbol: str
    price: int | None
    quantity: int
    remaining_quantity: int
    status: OrderStatus
    valid_until: datetime
    created_at: datetime
    trades: list[TradeInfo]

    model_config = {"from_attributes": True}


class PriceLevel(BaseModel):
    price: int
    total_quantity: int
    order_count: int


class OrderBook(BaseModel):
    symbol: str
    depth: int
    asks: list[PriceLevel]
    bids: list[PriceLevel]


class StockPrice(BaseModel):
    symbol: str
    last_price: int
    average_price: int
    trades_in_average: int


class BrokerBalance(BaseModel):
    broker_id: uuid.UUID
    broker_name: str
    balance: int


class BrokerRegister(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    webhook_url: str | None = Field(None, pattern=r"^https?://")


class BrokerRegistered(BaseModel):
    broker_id: uuid.UUID
    api_key: str


class WebhookPayload(BaseModel):
    event: str = "trade_executed"
    trade_id: uuid.UUID
    order_id: uuid.UUID
    symbol: str
    side: OrderSide
    price: int
    quantity: int
    order_remaining_quantity: int
    executed_at: datetime
