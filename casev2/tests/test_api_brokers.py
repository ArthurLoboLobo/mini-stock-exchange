"""Tests for broker registration endpoint (V2)."""
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine as create_sa_engine

from app.config import settings
from tests.conftest import TEST_ADMIN_KEY, admin_header, auth_header


@pytest.mark.asyncio
class TestBrokerRegistration:

    async def test_register_broker(self, client: AsyncClient):
        resp = await client.post(
            "/register",
            json={"name": "New Broker"},
            headers=admin_header(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "broker_id" in data
        assert data["api_key"].startswith("key-")

    async def test_register_broker_with_webhook(self, client: AsyncClient):
        resp = await client.post(
            "/register",
            json={"name": "Webhook Broker", "webhook_url": "http://example.com/hook"},
            headers=admin_header(),
        )
        assert resp.status_code == 201

        # Verify the webhook URL was actually persisted (response doesn't include it)
        broker_id = resp.json()["broker_id"]
        sa_engine = create_sa_engine(settings.database_url)
        async with sa_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT webhook_url FROM brokers WHERE id = :id"),
                {"id": broker_id},
            )
            assert result.scalar() == "http://example.com/hook"
        await sa_engine.dispose()

    async def test_registered_broker_can_trade(self, client: AsyncClient):
        """A broker created via /register should be able to use the API."""
        resp = await client.post(
            "/register",
            json={"name": "Trading Broker"},
            headers=admin_header(),
        )
        api_key = resp.json()["api_key"]

        balance = await client.get("/balance", headers=auth_header(api_key))
        assert balance.status_code == 200
        assert balance.json()["balance"] == 0

    async def test_register_no_auth(self, client: AsyncClient):
        resp = await client.post("/register", json={"name": "Broker"})
        assert resp.status_code == 403

    async def test_register_wrong_admin_key(self, client: AsyncClient):
        resp = await client.post(
            "/register",
            json={"name": "Broker"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    async def test_register_broker_key_rejected(self, client: AsyncClient, broker_with_key):
        """A regular broker key should not work for registration."""
        _, api_key = broker_with_key
        resp = await client.post(
            "/register",
            json={"name": "Broker"},
            headers=auth_header(api_key),
        )
        assert resp.status_code == 401

    async def test_register_empty_name(self, client: AsyncClient):
        resp = await client.post(
            "/register",
            json={"name": ""},
            headers=admin_header(),
        )
        assert resp.status_code == 422

    async def test_register_invalid_webhook_url(self, client: AsyncClient):
        resp = await client.post(
            "/register",
            json={"name": "Broker", "webhook_url": "not-a-url"},
            headers=admin_header(),
        )
        assert resp.status_code == 422
