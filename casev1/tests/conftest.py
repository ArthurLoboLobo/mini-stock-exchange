import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import hash_api_key, _broker_cache
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Broker

TEST_ADMIN_KEY = "test-admin-key"


@pytest_asyncio.fixture
async def db():
    """Per-test DB session with clean state.

    Creates its own engine so it's bound to the current test's event loop.
    Tables already exist from alembic (run by docker-compose on startup).
    """
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        # Clean all data before the test
        await session.execute(text("DELETE FROM trades"))
        await session.execute(text("DELETE FROM orders"))
        await session.execute(text("DELETE FROM brokers"))
        await session.commit()
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db):
    """Async HTTP client that uses the test DB session."""
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    original_admin_key = settings.admin_api_key
    settings.admin_api_key = TEST_ADMIN_KEY
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    settings.admin_api_key = original_admin_key
    _broker_cache.clear()


@pytest_asyncio.fixture
async def broker_with_key(db) -> tuple[Broker, str]:
    """Create a test broker and return (broker, raw_api_key)."""
    raw_key = f"test-key-{uuid.uuid4()}"
    broker = Broker(
        name="Test Broker",
        api_key_hash=hash_api_key(raw_key),
    )
    db.add(broker)
    await db.commit()
    await db.refresh(broker)
    return broker, raw_key


@pytest_asyncio.fixture
async def second_broker_with_key(db) -> tuple[Broker, str]:
    """Create a second test broker."""
    raw_key = f"test-key-{uuid.uuid4()}"
    broker = Broker(
        name="Second Broker",
        api_key_hash=hash_api_key(raw_key),
    )
    db.add(broker)
    await db.commit()
    await db.refresh(broker)
    return broker, raw_key


def auth_header(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def make_limit_order(**overrides) -> dict:
    """Build a valid limit order payload with sensible defaults."""
    valid_until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    order = {
        "document_number": "12345678901",
        "side": "bid",
        "order_type": "limit",
        "symbol": "PETR4",
        "price": 3500,
        "quantity": 1000,
        "valid_until": valid_until,
    }
    order.update(overrides)
    return order


def make_market_order(**overrides) -> dict:
    """Build a valid market order payload."""
    order = {
        "document_number": "12345678901",
        "side": "bid",
        "order_type": "market",
        "symbol": "PETR4",
        "quantity": 1000,
    }
    order.update(overrides)
    return order
