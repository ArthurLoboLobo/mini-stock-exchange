"""Tests for order API endpoints — validation, auth, access control."""
import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, make_limit_order, make_market_order


@pytest.mark.asyncio
class TestOrderCreation:

    async def test_create_limit_order(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.post(
            "/orders",
            json=make_limit_order(),
            headers=auth_header(key),
        )
        assert resp.status_code == 201
        assert "order_id" in resp.json()

    async def test_create_order_no_auth(self, client: AsyncClient):
        resp = await client.post("/orders", json=make_limit_order())
        assert resp.status_code == 403  # HTTPBearer returns 403 when header is missing

    async def test_create_order_bad_key(self, client: AsyncClient):
        resp = await client.post(
            "/orders",
            json=make_limit_order(),
            headers=auth_header("invalid-key"),
        )
        assert resp.status_code == 401

    async def test_limit_order_requires_price(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        order = make_limit_order()
        del order["price"]
        resp = await client.post("/orders", json=order, headers=auth_header(key))
        assert resp.status_code == 422

    async def test_limit_order_requires_valid_until(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        order = make_limit_order()
        del order["valid_until"]
        resp = await client.post("/orders", json=order, headers=auth_header(key))
        assert resp.status_code == 422

    async def test_market_order_rejects_price(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        order = make_market_order(price=1000)
        resp = await client.post("/orders", json=order, headers=auth_header(key))
        assert resp.status_code == 422

    async def test_negative_quantity_rejected(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        order = make_limit_order(quantity=-1)
        resp = await client.post("/orders", json=order, headers=auth_header(key))
        assert resp.status_code == 422

    async def test_zero_quantity_rejected(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        order = make_limit_order(quantity=0)
        resp = await client.post("/orders", json=order, headers=auth_header(key))
        assert resp.status_code == 422

    async def test_symbol_normalized_to_uppercase(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.post(
            "/orders",
            json=make_limit_order(symbol="petr4"),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]
        detail = await client.get(f"/orders/{order_id}", headers=auth_header(key))
        assert detail.json()["symbol"] == "PETR4"


@pytest.mark.asyncio
class TestOrderStatus:

    async def test_get_own_order(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.post(
            "/orders",
            json=make_limit_order(),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        detail = await client.get(f"/orders/{order_id}", headers=auth_header(key))
        assert detail.status_code == 200
        data = detail.json()
        assert data["id"] == order_id
        assert data["quantity"] == 1000
        assert data["remaining_quantity"] == 1000
        assert data["status"] == "open"

    async def test_get_nonexistent_order(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.get(
            "/orders/00000000-0000-0000-0000-000000000000",
            headers=auth_header(key),
        )
        assert resp.status_code == 404

    async def test_get_other_brokers_order(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        broker1, key1 = broker_with_key
        broker2, key2 = second_broker_with_key

        # Broker 1 creates an order
        resp = await client.post(
            "/orders",
            json=make_limit_order(),
            headers=auth_header(key1),
        )
        order_id = resp.json()["order_id"]

        # Broker 2 tries to access it
        detail = await client.get(f"/orders/{order_id}", headers=auth_header(key2))
        assert detail.status_code == 403


@pytest.mark.asyncio
class TestOrderCancel:

    async def test_cancel_open_order(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.post(
            "/orders", json=make_limit_order(), headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        resp = await client.post(f"/orders/{order_id}/cancel", headers=auth_header(key))
        assert resp.status_code == 204

        # Verify via GET
        get_data = (await client.get(f"/orders/{order_id}", headers=auth_header(key))).json()
        assert get_data["status"] == "closed"
        assert get_data["remaining_quantity"] == 1000

    async def test_cancel_nonexistent_order(self, client: AsyncClient, broker_with_key):
        _, key = broker_with_key
        resp = await client.post(
            "/orders/00000000-0000-0000-0000-000000000000/cancel",
            headers=auth_header(key),
        )
        assert resp.status_code == 204

    async def test_cancel_other_brokers_order(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        _, key1 = broker_with_key
        _, key2 = second_broker_with_key

        resp = await client.post(
            "/orders", json=make_limit_order(), headers=auth_header(key1),
        )
        order_id = resp.json()["order_id"]

        resp = await client.post(f"/orders/{order_id}/cancel", headers=auth_header(key2))
        assert resp.status_code == 403

    async def test_cancel_already_closed_order(self, client: AsyncClient, broker_with_key):
        _, key = broker_with_key
        resp = await client.post(
            "/orders", json=make_limit_order(), headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        # First cancel succeeds
        resp = await client.post(f"/orders/{order_id}/cancel", headers=auth_header(key))
        assert resp.status_code == 204

        # Second cancel is idempotent — returns 204
        resp = await client.post(f"/orders/{order_id}/cancel", headers=auth_header(key))
        assert resp.status_code == 204

        # Verify via GET
        get_data = (await client.get(f"/orders/{order_id}", headers=auth_header(key))).json()
        assert get_data["status"] == "closed"

    async def test_cancel_market_order(self, client: AsyncClient, broker_with_key):
        _, key = broker_with_key
        # Create liquidity so market order can fill
        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=3500, quantity=1000),
            headers=auth_header(key),
        )
        # Create market buy that fills against the ask
        resp = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=1000),
            headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        # Market orders are IOC — already closed, cancel is a no-op
        resp = await client.post(f"/orders/{order_id}/cancel", headers=auth_header(key))
        assert resp.status_code == 204

    async def test_cancel_partially_filled_order(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        _, key1 = broker_with_key
        _, key2 = second_broker_with_key

        # Broker1 posts ask for 100 shares
        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=3500, quantity=100),
            headers=auth_header(key1),
        )
        ask_id = resp.json()["order_id"]

        # Broker2 posts bid for 40 shares — partial fill
        await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=3500, quantity=40),
            headers=auth_header(key2),
        )

        # Cancel the partially filled ask
        resp = await client.post(f"/orders/{ask_id}/cancel", headers=auth_header(key1))
        assert resp.status_code == 204

        # Verify via GET
        get_data = (await client.get(f"/orders/{ask_id}", headers=auth_header(key1))).json()
        assert get_data["status"] == "closed"
        assert get_data["remaining_quantity"] == 60
        assert len(get_data["trades"]) == 1
        assert get_data["trades"][0]["quantity"] == 40

    async def test_cancelled_order_not_matched(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        _, key1 = broker_with_key
        _, key2 = second_broker_with_key

        # Broker1 posts ask
        resp = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=3500, quantity=100),
            headers=auth_header(key1),
        )
        ask_id = resp.json()["order_id"]

        # Cancel it
        resp = await client.post(f"/orders/{ask_id}/cancel", headers=auth_header(key1))
        assert resp.status_code == 204

        # Broker2 posts matching bid — should NOT match
        resp = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=3500, quantity=100),
            headers=auth_header(key2),
        )
        bid_id = resp.json()["order_id"]

        detail = await client.get(f"/orders/{bid_id}", headers=auth_header(key2))
        assert detail.json()["status"] == "open"
        assert detail.json()["remaining_quantity"] == 100
        assert detail.json()["trades"] == []

    async def test_cancel_no_auth(self, client: AsyncClient, broker_with_key):
        _, key = broker_with_key
        resp = await client.post(
            "/orders", json=make_limit_order(), headers=auth_header(key),
        )
        order_id = resp.json()["order_id"]

        resp = await client.post(f"/orders/{order_id}/cancel")
        assert resp.status_code == 403
