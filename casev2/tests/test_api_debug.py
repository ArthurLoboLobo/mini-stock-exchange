"""Tests for debug endpoints (V2)."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine as create_sa_engine

from app.config import settings
from app.engine import engine
from tests.conftest import (
    admin_header,
    auth_header,
    flush_persistence,
    make_limit_order,
)


async def test_reset_wipes_all_tables(client, broker_with_key):
    _, api_key = broker_with_key

    # Create an order so there's data in the orders table
    resp = await client.post(
        "/orders",
        json=make_limit_order(),
        headers=auth_header(api_key),
    )
    assert resp.status_code == 201

    # Flush to DB so data is persisted before reset
    await flush_persistence()

    # Reset
    resp = await client.post("/debug/reset", headers=admin_header())
    assert resp.status_code == 200
    assert resp.json() == {"status": "database reset"}

    # Verify all tables are empty via separate engine
    sa_engine = create_sa_engine(settings.database_url)
    async with sa_engine.connect() as conn:
        for table in ("trades", "orders", "brokers"):
            result = await conn.execute(text(f"SELECT count(*) FROM {table}"))
            assert result.scalar() == 0, f"{table} should be empty after reset"
    await sa_engine.dispose()


async def test_reset_requires_admin_key(client):
    resp = await client.post("/debug/reset")
    assert resp.status_code == 403


async def test_reset_rejects_broker_key(client, broker_with_key):
    _, api_key = broker_with_key
    resp = await client.post("/debug/reset", headers=auth_header(api_key))
    assert resp.status_code == 401


async def test_reset_clears_memory_and_db(client, broker_with_key):
    _, api_key = broker_with_key

    # Create an order
    resp = await client.post(
        "/orders",
        json=make_limit_order(),
        headers=auth_header(api_key),
    )
    assert resp.status_code == 201

    await flush_persistence()

    # Reset
    resp = await client.post("/debug/reset", headers=admin_header())
    assert resp.status_code == 200

    # Verify in-memory state is clear
    assert len(engine.orders) == 0
    assert not engine.book.asks
    assert not engine.book.bids
    assert len(engine.brokers_by_key_hash) == 0

    # Verify persistence task was restarted
    assert engine.persistence_task is not None

    # Verify DB tables are empty
    sa_engine = create_sa_engine(settings.database_url)
    async with sa_engine.connect() as conn:
        for table in ("trades", "orders", "brokers"):
            result = await conn.execute(text(f"SELECT count(*) FROM {table}"))
            assert result.scalar() == 0, f"{table} should be empty after reset"
    await sa_engine.dispose()
