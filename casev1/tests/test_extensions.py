"""Tests for extension endpoints: stock price, order book, broker balance."""
import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, make_limit_order


@pytest.mark.asyncio
class TestStockPrice:

    async def test_price_after_trades(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key

        # Create a trade: sell at 1000, buy at 1000
        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=100),
            headers=auth_header(key),
        )
        await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=100),
            headers=auth_header(key),
        )

        resp = await client.get("/stocks/PETR4/price", headers=auth_header(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "PETR4"
        assert data["last_price"] == 1000

    async def test_price_no_trades(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.get("/stocks/NOTRADED/price", headers=auth_header(key))
        assert resp.status_code == 404

    async def test_price_no_auth(self, client: AsyncClient):
        resp = await client.get("/stocks/PETR4/price")
        assert resp.status_code == 403


@pytest.mark.asyncio
class TestOrderBook:

    async def test_book_with_orders(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        symbol = "BOOKTEST"

        # Place some asks
        for price in [1100, 1200, 1300]:
            await client.post(
                "/orders",
                json=make_limit_order(side="ask", price=price, quantity=500, symbol=symbol),
                headers=auth_header(key),
            )

        # Place some bids
        for price in [900, 800, 700]:
            await client.post(
                "/orders",
                json=make_limit_order(side="bid", price=price, quantity=500, symbol=symbol),
                headers=auth_header(key),
            )

        resp = await client.get(f"/stocks/{symbol}/book", headers=auth_header(key))
        assert resp.status_code == 200
        data = resp.json()

        assert data["symbol"] == symbol
        assert len(data["asks"]) == 3
        assert len(data["bids"]) == 3

        # Asks sorted low to high
        assert data["asks"][0]["price"] == 1100
        assert data["asks"][2]["price"] == 1300

        # Bids sorted high to low
        assert data["bids"][0]["price"] == 900
        assert data["bids"][2]["price"] == 700

    async def test_book_aggregation(self, client: AsyncClient, broker_with_key):
        """Multiple orders at the same price level should be aggregated."""
        broker, key = broker_with_key
        symbol = "AGGTEST"

        # Two asks at the same price
        for _ in range(2):
            await client.post(
                "/orders",
                json=make_limit_order(side="ask", price=1000, quantity=500, symbol=symbol),
                headers=auth_header(key),
            )

        resp = await client.get(f"/stocks/{symbol}/book", headers=auth_header(key))
        data = resp.json()

        assert len(data["asks"]) == 1
        assert data["asks"][0]["price"] == 1000
        assert data["asks"][0]["total_quantity"] == 1000
        assert data["asks"][0]["order_count"] == 2

    async def test_book_empty(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.get("/stocks/EMPTY99/book", headers=auth_header(key))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["asks"]) == 0
        assert len(data["bids"]) == 0


@pytest.mark.asyncio
class TestBrokerBalance:

    async def test_balance_after_trades(
        self, client: AsyncClient, broker_with_key, second_broker_with_key
    ):
        broker1, key1 = broker_with_key
        broker2, key2 = second_broker_with_key

        # Broker 1 sells, Broker 2 buys
        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=100),
            headers=auth_header(key1),
        )
        await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=100),
            headers=auth_header(key2),
        )

        # Broker 1 should have positive balance (sold 100 * 1000 cents = 100000)
        resp1 = await client.get("/balance", headers=auth_header(key1))
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["balance"] == 100000

        # Broker 2 should have negative balance (bought 100 * 1000 cents = 100000)
        resp2 = await client.get("/balance", headers=auth_header(key2))
        data2 = resp2.json()
        assert data2["balance"] == -100000

    async def test_balance_no_trades(self, client: AsyncClient, broker_with_key):
        broker, key = broker_with_key
        resp = await client.get("/balance", headers=auth_header(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["balance"] == 0
