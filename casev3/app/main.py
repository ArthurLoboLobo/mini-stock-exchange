import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.database import async_session
from app.engine import BrokerInfo, Order, Trade, engine
from app.engine.persistence import run_persistence_loop
from app.middleware import SlowRequestMiddleware
from app.models import Broker
from app.models import Order as DBOrder
from app.models import OrderStatus
from app.routers import brokers, debug, orders, stocks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Starting up...")

    # 1. Load full broker info
    async with async_session() as session:
        result = await session.execute(
            select(Broker.id, Broker.name, Broker.api_key_hash, Broker.webhook_url, Broker.balance)
        )
        for broker_id, name, key_hash, webhook_url, balance in result.all():
            engine.brokers_by_key_hash[key_hash] = broker_id
            engine.brokers[broker_id] = BrokerInfo(
                name=name, balance=balance, webhook_url=webhook_url,
            )
        logger.info(f"Loaded {len(engine.brokers)} brokers into memory")

    # 2. Load open orders
    async with async_session() as session:
        query = (
            select(DBOrder)
            .where(
                DBOrder.status == OrderStatus.open,
                DBOrder.valid_until > func.now()
            )
            .order_by(DBOrder.created_at.asc())
        )
        result = await session.execute(query)

        open_order_ids: list = []
        for db_order in result.scalars().all():
            order = Order(
                id=db_order.id,
                broker_id=db_order.broker_id,
                symbol=db_order.symbol,
                side=db_order.side,
                order_type=db_order.order_type,
                price=db_order.price,
                quantity=db_order.quantity,
                remaining_quantity=db_order.remaining_quantity,
                status=db_order.status,
                document_number=db_order.document_number,
                valid_until=db_order.valid_until,
                created_at=db_order.created_at,
            )
            engine.orders[order.id] = order
            engine.book.insert(order)
            open_order_ids.append(order.id)

        logger.info(f"Loaded {len(open_order_ids)} open orders into memory")

    # 3. Load trades for open orders
    if open_order_ids:
        async with async_session() as session:
            result = await session.execute(
                text(
                    "SELECT id, buy_order_id, sell_order_id, symbol, price, quantity, created_at "
                    "FROM trades WHERE buy_order_id = ANY(:ids) OR sell_order_id = ANY(:ids)"
                ),
                {"ids": [str(oid) for oid in open_order_ids]},
            )
            trade_count = 0
            for row in result.fetchall():
                t = Trade(
                    id=row[0], buy_order_id=row[1], sell_order_id=row[2],
                    symbol=row[3], price=row[4], quantity=row[5],
                    buyer_broker_id=engine.orders[row[1]].broker_id if row[1] in engine.orders else row[1],
                    seller_broker_id=engine.orders[row[2]].broker_id if row[2] in engine.orders else row[2],
                    created_at=row[6],
                )
                engine.trades_by_order.setdefault(t.buy_order_id, []).append(t)
                engine.trades_by_order.setdefault(t.sell_order_id, []).append(t)
                trade_count += 1
            logger.info(f"Loaded {trade_count} trades for open orders into memory")

    # 4. Load recent trade prices
    async with async_session() as session:
        result = await session.execute(
            text("SELECT symbol, price FROM trades ORDER BY created_at DESC")
        )
        symbol_counts: dict[str, int] = {}
        # Collect in reverse order, then reverse per-symbol
        temp: dict[str, list[int]] = {}
        for symbol, price in result.fetchall():
            count = symbol_counts.get(symbol, 0)
            if count >= 1000:
                continue
            symbol_counts[symbol] = count + 1
            temp.setdefault(symbol, []).append(price)
        for symbol, prices in temp.items():
            prices.reverse()  # oldest first
            engine.trade_prices[symbol] = deque(prices, maxlen=1000)
        logger.info(f"Loaded trade prices for {len(engine.trade_prices)} symbols into memory")

    # Start persistence loop
    engine.persistence_task = asyncio.create_task(
        run_persistence_loop(engine, async_session)
    )
    logger.info("Persistence loop started")

    yield

    # --- Shutdown ---
    logger.info("Shutting down...")
    if engine.persistence_task:
        engine.persistence_task.cancel()
        try:
            await engine.persistence_task
        except asyncio.CancelledError:
            pass
        logger.info("Persistence loop stopped")


app = FastAPI(title="Mini Stock Exchange", version="3.0.0", lifespan=lifespan)

# Add middleware
app.add_middleware(SlowRequestMiddleware)

# Include routers
app.include_router(orders.router)
app.include_router(stocks.router)
app.include_router(brokers.router)
app.include_router(debug.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
