import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import func, select

from app.database import async_session
from app.engine import Order, engine
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

    # Load broker auth
    async with async_session() as session:
        result = await session.execute(select(Broker.id, Broker.api_key_hash))
        for broker_id, key_hash in result.all():
            engine.brokers_by_key_hash[key_hash] = broker_id
        logger.info(f"Loaded {len(engine.brokers_by_key_hash)} brokers into memory")

    # Load open orders
    async with async_session() as session:
        # Load only open orders that are not expired
        query = (
            select(DBOrder)
            .where(
                DBOrder.status == OrderStatus.open,
                DBOrder.valid_until > func.now()
            )
            .order_by(DBOrder.created_at.asc())
        )
        result = await session.execute(query)

        loaded_count = 0
        for db_order in result.scalars().all():
            # Create in-memory Order object
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

            # Add to engine
            engine.orders[order.id] = order
            engine.book.insert(order)
            loaded_count += 1

        logger.info(f"Loaded {loaded_count} open orders into memory")

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


app = FastAPI(title="Mini Stock Exchange", version="2.0.0", lifespan=lifespan)

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
