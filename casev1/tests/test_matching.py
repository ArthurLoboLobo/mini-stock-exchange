"""Tests for the matching engine — covers all case spec scenarios."""
import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, make_limit_order, make_market_order


@pytest.mark.asyncio
class TestBasicMatching:
    """Case spec examples 1-3."""

    async def test_same_price_match(self, client: AsyncClient, broker_with_key):
        """Example 1: A sells 1000 at $10, B buys 1000 at $10 → match at $10."""
        broker, key = broker_with_key

        # A sells
        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        assert sell.status_code == 201
        sell_id = sell.json()["order_id"]

        # B buys at same price
        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        assert buy.status_code == 201
        buy_id = buy.json()["order_id"]

        # Check sell order — should be fully filled
        sell_status = await client.get(f"/orders/{sell_id}", headers=auth_header(key))
        assert sell_status.status_code == 200
        sell_data = sell_status.json()
        assert sell_data["remaining_quantity"] == 0
        assert sell_data["status"] == "closed"
        assert len(sell_data["trades"]) == 1
        assert sell_data["trades"][0]["price"] == 1000
        assert sell_data["trades"][0]["quantity"] == 1000

        # Check buy order — should be fully filled
        buy_status = await client.get(f"/orders/{buy_id}", headers=auth_header(key))
        buy_data = buy_status.json()
        assert buy_data["remaining_quantity"] == 0
        assert buy_data["status"] == "closed"

    async def test_no_match_price_gap(self, client: AsyncClient, broker_with_key):
        """Example 2: A sells at $20, B buys at $10 → no match."""
        broker, key = broker_with_key

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=2000, quantity=1000),
            headers=auth_header(key),
        )
        assert sell.status_code == 201
        sell_id = sell.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        assert buy.status_code == 201
        buy_id = buy.json()["order_id"]

        # Both should remain open with full quantity
        sell_status = await client.get(f"/orders/{sell_id}", headers=auth_header(key))
        assert sell_status.json()["remaining_quantity"] == 1000
        assert sell_status.json()["status"] == "open"

        buy_status = await client.get(f"/orders/{buy_id}", headers=auth_header(key))
        assert buy_status.json()["remaining_quantity"] == 1000
        assert buy_status.json()["status"] == "open"

    async def test_price_gap_uses_seller_price(self, client: AsyncClient, broker_with_key):
        """Example 3: A sells at $10, B buys at $20 → match at $10 (seller's price)."""
        broker, key = broker_with_key

        # A sells at 1000 cents
        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        # B buys at 2000 cents — buyer is willing to pay more
        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=2000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        # Execution price should be seller's price (1000)
        buy_status = await client.get(f"/orders/{buy_id}", headers=auth_header(key))
        buy_data = buy_status.json()
        assert buy_data["trades"][0]["price"] == 1000

    async def test_seller_price_when_sell_is_incoming(self, client: AsyncClient, broker_with_key):
        """When a SELL comes in matching a resting BID, execution price = seller's price."""
        broker, key = broker_with_key

        # Resting BID at 2000
        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=2000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        # Incoming ASK at 1000
        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        # Execution price should be seller's price (1000), not buyer's (2000)
        sell_status = await client.get(f"/orders/{sell_id}", headers=auth_header(key))
        assert sell_status.json()["trades"][0]["price"] == 1000


@pytest.mark.asyncio
class TestPartialFills:

    async def test_partial_fill_buyer_larger(self, client: AsyncClient, broker_with_key):
        """A sells 500, B buys 1000 → A fully filled, B has 500 remaining."""
        broker, key = broker_with_key

        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )
        sell_id = sell.json()["order_id"]

        buy = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        sell_data = (await client.get(f"/orders/{sell_id}", headers=auth_header(key))).json()
        assert sell_data["remaining_quantity"] == 0
        assert sell_data["status"] == "closed"

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 500
        assert buy_data["status"] == "open"

    async def test_multiple_sellers_partial_fill(self, client: AsyncClient, broker_with_key):
        """A sells 500, B sells 500, C buys 1500 → A and B fully filled, C has 500 left."""
        broker, key = broker_with_key

        sell_a = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )
        sell_a_id = sell_a.json()["order_id"]

        sell_b = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )
        sell_b_id = sell_b.json()["order_id"]

        buy_c = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1500),
            headers=auth_header(key),
        )
        buy_c_id = buy_c.json()["order_id"]

        # A fully filled
        a_data = (await client.get(f"/orders/{sell_a_id}", headers=auth_header(key))).json()
        assert a_data["remaining_quantity"] == 0
        assert a_data["status"] == "closed"

        # B fully filled
        b_data = (await client.get(f"/orders/{sell_b_id}", headers=auth_header(key))).json()
        assert b_data["remaining_quantity"] == 0
        assert b_data["status"] == "closed"

        # C has 500 remaining
        c_data = (await client.get(f"/orders/{buy_c_id}", headers=auth_header(key))).json()
        assert c_data["remaining_quantity"] == 500
        assert c_data["status"] == "open"
        assert len(c_data["trades"]) == 2


@pytest.mark.asyncio
class TestFIFOOrdering:

    async def test_fifo_same_price(self, client: AsyncClient, broker_with_key):
        """A and B both sell at $10, C buys 1000 → first seller gets matched."""
        broker, key = broker_with_key

        sell_a = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_a_id = sell_a.json()["order_id"]

        sell_b = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_b_id = sell_b.json()["order_id"]

        # C buys 1000 — should match A (first), not B
        buy_c = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1000, quantity=1000),
            headers=auth_header(key),
        )

        # A should be fully filled
        a_data = (await client.get(f"/orders/{sell_a_id}", headers=auth_header(key))).json()
        assert a_data["remaining_quantity"] == 0
        assert a_data["status"] == "closed"

        # B should still be open
        b_data = (await client.get(f"/orders/{sell_b_id}", headers=auth_header(key))).json()
        assert b_data["remaining_quantity"] == 1000
        assert b_data["status"] == "open"


@pytest.mark.asyncio
class TestPriceTimePriority:

    async def test_best_price_wins(self, client: AsyncClient, broker_with_key):
        """A sells at $12, B sells at $10, C buys at $15 → B gets matched (lower ask)."""
        broker, key = broker_with_key

        sell_a = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1200, quantity=1000),
            headers=auth_header(key),
        )
        sell_a_id = sell_a.json()["order_id"]

        sell_b = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=1000),
            headers=auth_header(key),
        )
        sell_b_id = sell_b.json()["order_id"]

        buy_c = await client.post(
            "/orders",
            json=make_limit_order(side="bid", price=1500, quantity=1000),
            headers=auth_header(key),
        )
        buy_c_id = buy_c.json()["order_id"]

        # B (cheaper) should be matched, not A
        b_data = (await client.get(f"/orders/{sell_b_id}", headers=auth_header(key))).json()
        assert b_data["remaining_quantity"] == 0

        a_data = (await client.get(f"/orders/{sell_a_id}", headers=auth_header(key))).json()
        assert a_data["remaining_quantity"] == 1000

        # Execution price should be B's price (seller's price = 1000)
        c_data = (await client.get(f"/orders/{buy_c_id}", headers=auth_header(key))).json()
        assert c_data["trades"][0]["price"] == 1000


@pytest.mark.asyncio
class TestMarketOrders:

    async def test_market_buy_fills_immediately(self, client: AsyncClient, broker_with_key):
        """Market buy fills against resting asks."""
        broker, key = broker_with_key

        # Resting ask
        sell = await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=500),
            headers=auth_header(key),
        )

        # Market buy
        buy = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=500),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 0
        assert buy_data["status"] == "closed"
        assert buy_data["trades"][0]["price"] == 1000

    async def test_market_order_ioc_partial(self, client: AsyncClient, broker_with_key):
        """Market order partially fills and closes (IOC)."""
        broker, key = broker_with_key

        # Only 300 available
        await client.post(
            "/orders",
            json=make_limit_order(side="ask", price=1000, quantity=300),
            headers=auth_header(key),
        )

        # Market buy for 1000
        buy = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=1000),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 700  # only 300 filled
        assert buy_data["status"] == "closed"  # IOC: closed regardless

    async def test_market_order_no_liquidity(self, client: AsyncClient, broker_with_key):
        """Market order with no matching orders → closed with nothing filled."""
        broker, key = broker_with_key

        buy = await client.post(
            "/orders",
            json=make_market_order(side="bid", quantity=1000, symbol="ZZZZ9"),
            headers=auth_header(key),
        )
        buy_id = buy.json()["order_id"]

        buy_data = (await client.get(f"/orders/{buy_id}", headers=auth_header(key))).json()
        assert buy_data["remaining_quantity"] == 1000
        assert buy_data["status"] == "closed"
        assert len(buy_data["trades"]) == 0
