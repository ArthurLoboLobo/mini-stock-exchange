"""Tests for broker registration endpoint."""
import pytest
from httpx import AsyncClient

from tests.conftest import TEST_ADMIN_KEY, auth_header


def admin_header() -> dict:
    return {"Authorization": f"Bearer {TEST_ADMIN_KEY}"}


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

    async def test_registered_broker_can_trade(self, client: AsyncClient):
        """A broker created via /register should be able to use the API."""
        resp = await client.post(
            "/register",
            json={"name": "Trading Broker"},
            headers=admin_header(),
        )
        api_key = resp.json()["api_key"]

        # Use the new key to check balance
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
        _, key = broker_with_key
        resp = await client.post(
            "/register",
            json={"name": "Broker"},
            headers=auth_header(key),
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
