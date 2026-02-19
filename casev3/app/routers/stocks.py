import itertools

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_broker_id
from app.engine import engine
from app.schemas import OrderBook, PriceLevel, StockPrice

router = APIRouter()


@router.get("/stocks/{symbol}/price", response_model=StockPrice)
async def get_stock_price(
    symbol: str,
    trades: int = Query(default=50, ge=1, le=1000),
    _broker_id=Depends(get_current_broker_id),
):
    symbol = symbol.upper()

    prices_deque = engine.trade_prices.get(symbol)
    if not prices_deque:
        raise HTTPException(status_code=404, detail="No trades found for symbol")

    recent = list(prices_deque)[-trades:]
    last_price = recent[-1]
    average_price = sum(recent) // len(recent)

    return StockPrice(
        symbol=symbol,
        last_price=last_price,
        average_price=average_price,
        trades_in_average=len(recent),
    )


@router.get("/stocks/{symbol}/book", response_model=OrderBook)
async def get_order_book(
    symbol: str,
    depth: int = Query(default=10, ge=1, le=50),
    _broker_id=Depends(get_current_broker_id),
):
    symbol = symbol.upper()

    # Asks: lowest price first
    symbol_asks = engine.book.asks.get(symbol)
    asks = []
    if symbol_asks:
        for price, dq in itertools.islice(symbol_asks.items(), depth):
            asks.append(PriceLevel(
                price=price,
                total_quantity=sum(o.remaining_quantity for o in dq),
                order_count=len(dq),
            ))

    # Bids: highest price first
    symbol_bids = engine.book.bids.get(symbol)
    bids = []
    if symbol_bids:
        for price, dq in itertools.islice(reversed(symbol_bids.items()), depth):
            bids.append(PriceLevel(
                price=price,
                total_quantity=sum(o.remaining_quantity for o in dq),
                order_count=len(dq),
            ))

    return OrderBook(symbol=symbol, depth=depth, asks=asks, bids=bids)
