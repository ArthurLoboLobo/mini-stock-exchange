import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.middleware import SlowRequestMiddleware
from app.routers import orders, stocks, brokers, debug
from app.tasks import start_expiration_cleanup, stop_expiration_cleanup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_expiration_cleanup()
    yield
    await stop_expiration_cleanup()


app = FastAPI(title="Mini Stock Exchange", version="1.0.0", lifespan=lifespan)
app.add_middleware(SlowRequestMiddleware)
app.include_router(orders.router)
app.include_router(stocks.router)
app.include_router(brokers.router)
app.include_router(debug.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
