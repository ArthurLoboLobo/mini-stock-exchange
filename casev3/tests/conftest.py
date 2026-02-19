import asyncio
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine as create_sa_engine

from app.config import settings
from app import database as db_module
from app.database import async_session
from app.engine import engine
from app.engine.persistence import flush_batch, run_persistence_loop
from app.main import app

TEST_ADMIN_KEY = "test-admin-key"


async def flush_persistence():
    """Drain the engine queue and flush all pending items to DB.

    No await between drain and flush — safe from background task
    interleaving since asyncio is single-threaded.
    """
    items = []
    while not engine.queue.empty():
        try:
            items.append(engine.queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if items:
        try:
            await flush_batch(items, async_session, engine)
        finally:
            for _ in items:
                engine.queue.task_done()


async def _cancel_persistence_task():
    """Cancel engine.persistence_task if it exists.

    Handles tasks from the current loop (cancel + await) and stale tasks
    from a previous test's closed event loop (just drop the reference).
    """
    if engine.persistence_task is None:
        return
    current_loop = asyncio.get_running_loop()
    task_loop = engine.persistence_task.get_loop()
    if task_loop is current_loop and not engine.persistence_task.done():
        engine.persistence_task.cancel()
        try:
            await engine.persistence_task
        except (asyncio.CancelledError, RuntimeError):
            pass
    engine.persistence_task = None


@pytest_asyncio.fixture
async def clean_state():
    """Per-test: truncate DB, reset engine memory, set admin key."""
    # 0. Dispose app's SA engine pool — each test function gets its own
    #    event loop, so pooled connections from the previous test are stale.
    await db_module.engine.dispose()

    # 1. Cancel any leftover persistence task (may be from a dead event loop)
    await _cancel_persistence_task()

    # 2. Truncate DB via a throwaway engine (independent of app pool)
    sa_engine = create_sa_engine(settings.database_url)
    async with sa_engine.begin() as conn:
        await conn.execute(text("DELETE FROM trades"))
        await conn.execute(text("DELETE FROM orders"))
        await conn.execute(text("DELETE FROM brokers"))
    await sa_engine.dispose()

    # 3. Clear in-memory state + fresh queue
    #    engine.clear() drains queue with get_nowait() but never calls
    #    task_done(), leaving _unfinished_tasks wrong. Replacing the queue
    #    avoids queue.join() hanging in Option B tests.
    engine.clear()
    engine.queue = asyncio.Queue()

    # 4. Set admin key for tests
    original_admin_key = settings.admin_api_key
    settings.admin_api_key = TEST_ADMIN_KEY

    yield

    # Teardown: cancel persistence task (e.g. started by POST /debug/reset)
    await _cancel_persistence_task()
    engine.clear()
    engine.queue = asyncio.Queue()
    settings.admin_api_key = original_admin_key


@pytest_asyncio.fixture
async def client(clean_state):
    """Async HTTP client. ASGITransport does NOT trigger lifespan, so the
    persistence loop is NOT running. Use flush_persistence() to push items
    to DB, or the with_persistence_loop fixture for Option B tests.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def with_persistence_loop(client):
    """Start the background persistence loop for tests that need it (Option B)."""
    engine.persistence_task = asyncio.create_task(
        run_persistence_loop(engine, async_session)
    )
    yield
    if engine.persistence_task and not engine.persistence_task.done():
        engine.persistence_task.cancel()
        try:
            await engine.persistence_task
        except asyncio.CancelledError:
            pass
        engine.persistence_task = None


@pytest_asyncio.fixture
async def broker_with_key(client) -> tuple[str, str]:
    """Register a test broker via API. Returns (broker_id, api_key)."""
    resp = await client.post(
        "/register",
        json={"name": "Test Broker"},
        headers=admin_header(),
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["broker_id"], data["api_key"]


@pytest_asyncio.fixture
async def second_broker_with_key(client) -> tuple[str, str]:
    """Register a second test broker via API. Returns (broker_id, api_key)."""
    resp = await client.post(
        "/register",
        json={"name": "Second Broker"},
        headers=admin_header(),
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["broker_id"], data["api_key"]


def admin_header() -> dict:
    return {"Authorization": f"Bearer {TEST_ADMIN_KEY}"}


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
