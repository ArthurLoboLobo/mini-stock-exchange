from sqlalchemy import text

from tests.conftest import TEST_ADMIN_KEY, auth_header, make_limit_order


async def test_reset_wipes_all_tables(client, db, broker_with_key):
    broker, api_key = broker_with_key

    # Create an order so there's data in orders table
    resp = await client.post("/orders", json=make_limit_order(), headers=auth_header(api_key))
    assert resp.status_code == 201

    # Reset
    resp = await client.post("/debug/reset", headers=auth_header(TEST_ADMIN_KEY))
    assert resp.status_code == 200
    assert resp.json() == {"status": "database reset"}

    # Verify all tables are empty
    for table in ("trades", "orders", "brokers"):
        result = await db.execute(text(f"SELECT count(*) FROM {table}"))
        assert result.scalar() == 0, f"{table} should be empty after reset"


async def test_reset_requires_admin_key(client):
    resp = await client.post("/debug/reset")
    assert resp.status_code in (401, 403)


async def test_reset_rejects_broker_key(client, broker_with_key):
    _, api_key = broker_with_key
    resp = await client.post("/debug/reset", headers=auth_header(api_key))
    assert resp.status_code in (401, 403)
