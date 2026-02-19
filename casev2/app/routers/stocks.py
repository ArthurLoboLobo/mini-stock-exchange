from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_broker_id
from app.database import get_db
from app.models import Order, OrderSide, OrderStatus, Trade
from app.schemas import OrderBook, PriceLevel, StockPrice

router = APIRouter()


@router.get("/stocks/{symbol}/price", response_model=StockPrice)
async def get_stock_price(
    symbol: str,
    trades: int = Query(default=50, ge=1, le=1000),
    _broker_id=Depends(get_current_broker_id),
    db: AsyncSession = Depends(get_db),
):
    symbol = symbol.upper()

    recent_query = (
        select(Trade.price)
        .where(Trade.symbol == symbol)
        .order_by(Trade.created_at.desc())
        .limit(trades)
    )
    recent_result = await db.execute(recent_query)
    recent_prices = recent_result.scalars().all()

    if not recent_prices:
        raise HTTPException(status_code=404, detail="No trades found for symbol")

    last_price = recent_prices[0]
    average_price = sum(recent_prices) // len(recent_prices)

    return StockPrice(
        symbol=symbol,
        last_price=last_price,
        average_price=average_price,
        trades_in_average=len(recent_prices),
    )


@router.get("/stocks/{symbol}/book", response_model=OrderBook)
async def get_order_book(
    symbol: str,
    depth: int = Query(default=10, ge=1, le=50),
    _broker_id=Depends(get_current_broker_id),
    db: AsyncSession = Depends(get_db),
):
    symbol = symbol.upper()

    # Asks: lowest price first
    asks_query = (
        select(
            Order.price,
            func.sum(Order.remaining_quantity).label("total_quantity"),
            func.count().label("order_count"),
        )
        .where(
            Order.symbol == symbol,
            Order.side == OrderSide.ask,
            Order.status == OrderStatus.open,
            Order.valid_until > func.now(),
        )
        .group_by(Order.price)
        .order_by(Order.price.asc())
        .limit(depth)
    )
    asks_result = await db.execute(asks_query)
    asks = [
        PriceLevel(price=row.price, total_quantity=row.total_quantity, order_count=row.order_count)
        for row in asks_result
    ]

    # Bids: highest price first
    bids_query = (
        select(
            Order.price,
            func.sum(Order.remaining_quantity).label("total_quantity"),
            func.count().label("order_count"),
        )
        .where(
            Order.symbol == symbol,
            Order.side == OrderSide.bid,
            Order.status == OrderStatus.open,
            Order.valid_until > func.now(),
        )
        .group_by(Order.price)
        .order_by(Order.price.desc())
        .limit(depth)
    )
    bids_result = await db.execute(bids_query)
    bids = [
        PriceLevel(price=row.price, total_quantity=row.total_quantity, order_count=row.order_count)
        for row in bids_result
    ]

    return OrderBook(symbol=symbol, depth=depth, asks=asks, bids=bids)
