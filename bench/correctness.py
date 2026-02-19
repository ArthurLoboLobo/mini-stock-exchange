"""
Correctness tests for the mini stock exchange.

15 sequential tests verifying the system produces correct results.
No timing, no concurrency pressure. Output is pass/fail.

Usage:
    python correctness.py --version v1
    python correctness.py --version v1 --url http://localhost:8000
"""
import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from shared import (
    DEFAULT_ADMIN_KEY,
    DEFAULT_URL,
    GREEN,
    RED,
    BOLD,
    RESET,
    pass_str,
    fail_str,
    admin_headers,
    broker_header,
    limit_order,
    market_order,
    poll,
    reset_db,
    save_results,
    WebhookSink,
)


DISPLAY_NAMES = {
    "balance_invariant": "Balance Invariant",
    "execution_price_rule": "Execution Price Rule",
    "price_time_priority": "Price-Time Priority",
    "partial_fill_single": "Partial Fill (Single)",
    "partial_fill_multi": "Partial Fill (Multi)",
    "no_match": "No Match",
    "expiration": "Expiration",
    "market_order_match": "Market Order Match",
    "market_order_ioc_cancel": "Market Order IOC Cancel",
    "market_order_partial_ioc": "Market Order Partial IOC",
    "order_book_state": "Order Book State",
    "concurrent_contention": "Concurrent Contention",
    "webhook_delivery": "Webhook Delivery",
    "cancel_order": "Cancel Order",
    "stock_price": "Stock Price",
}


class CorrectnessRunner:
    def __init__(self, url: str, admin_key: str, version: str):
        self.url = url
        self.admin_key = admin_key
        self.version = version
        self.results: list[dict] = []

    async def _register_broker(self, client: httpx.AsyncClient, name: str, webhook_url: str | None = None) -> str:
        body = {"name": name}
        if webhook_url:
            body["webhook_url"] = webhook_url
        resp = await client.post("/register", json=body, headers=admin_headers(self.admin_key))
        assert resp.status_code == 201, f"register failed: {resp.status_code} {resp.text}"
        return resp.json()["api_key"]

    async def _submit_order(self, client: httpx.AsyncClient, key: str, order: dict) -> str:
        resp = await client.post("/orders", json=order, headers=broker_header(key))
        assert resp.status_code == 201, f"submit failed: {resp.status_code} {resp.text}"
        return resp.json()["order_id"]

    async def _get_order(self, client: httpx.AsyncClient, key: str, order_id: str) -> dict:
        resp = await client.get(f"/orders/{order_id}", headers=broker_header(key))
        assert resp.status_code == 200, f"get order failed: {resp.status_code}"
        return resp.json()

    async def _get_balance(self, client: httpx.AsyncClient, key: str) -> int:
        resp = await client.get("/balance", headers=broker_header(key))
        assert resp.status_code == 200
        return resp.json()["balance"]

    async def _get_book(self, client: httpx.AsyncClient, key: str, symbol: str) -> dict:
        resp = await client.get(f"/stocks/{symbol}/book", headers=broker_header(key))
        assert resp.status_code == 200
        return resp.json()

    async def run_all(self):
        tests = [
            ("balance_invariant", self.test_balance_invariant),
            ("execution_price_rule", self.test_execution_price_rule),
            ("price_time_priority", self.test_price_time_priority),
            ("partial_fill_single", self.test_partial_fill_single),
            ("partial_fill_multi", self.test_partial_fill_multi),
            ("no_match", self.test_no_match),
            ("expiration", self.test_expiration),
            ("market_order_match", self.test_market_order_match),
            ("market_order_ioc_cancel", self.test_market_order_ioc_cancel),
            ("market_order_partial_ioc", self.test_market_order_partial_ioc),
            ("order_book_state", self.test_order_book_state),
            ("concurrent_contention", self.test_concurrent_contention),
            ("webhook_delivery", self.test_webhook_delivery),
            ("cancel_order", self.test_cancel_order),
            ("stock_price", self.test_stock_price),
        ]

        print(f"\n{BOLD}Correctness Tests{RESET}")
        print(f"  Version: {self.version}")
        passed = 0
        failed = 0

        for name, test_fn in tests:
            display = DISPLAY_NAMES.get(name, name)
            async with httpx.AsyncClient(base_url=self.url, timeout=30.0) as client:
                await reset_db(client, self.admin_key)
                try:
                    await test_fn(client)
                    print(f"  [{pass_str()}] {display}")
                    self.results.append({"name": name, "status": "pass"})
                    passed += 1
                except Exception as e:
                    print(f"  [{fail_str()}] {display} — {e}")
                    self.results.append({"name": name, "status": "fail", "error": str(e)})
                    failed += 1

        total = passed + failed
        color = GREEN if failed == 0 else RED
        print(f"\n  {color}{passed}/{total} passed, {failed} failed{RESET}\n")
        return passed, failed

    # ------------------------------------------------------------------
    # Test 1: Balance invariant
    # ------------------------------------------------------------------
    async def test_balance_invariant(self, client: httpx.AsyncClient):
        keys = []
        for i in range(4):
            keys.append(await self._register_broker(client, f"BalBroker{i}"))

        # Multiple trades across different symbols
        for symbol in ["BAL1", "BAL2"]:
            # Seller at 1000
            await self._submit_order(client, keys[0], limit_order("ask", 1000, 200, symbol))
            # Buyer at 1000
            oid = await self._submit_order(client, keys[1], limit_order("bid", 1000, 200, symbol))
            # Wait for trade
            await poll(lambda oid=oid, k=keys[1]: self._check_order_traded(client, k, oid))

            await self._submit_order(client, keys[2], limit_order("ask", 1500, 100, symbol))
            oid = await self._submit_order(client, keys[3], limit_order("bid", 1500, 100, symbol))
            await poll(lambda oid=oid, k=keys[3]: self._check_order_traded(client, k, oid))

        # Sum all balances
        total = 0
        for key in keys:
            total += await self._get_balance(client, key)
        assert total == 0, f"SUM(balance) = {total}, expected 0"

    async def _check_order_traded(self, client, key, order_id) -> bool:
        try:
            order = await self._get_order(client, key, order_id)
            return len(order.get("trades", [])) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Test 2: Execution price rule
    # ------------------------------------------------------------------
    async def test_execution_price_rule(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_key = await self._register_broker(client, "Buyer")

        # Seller asks at 1000, buyer bids at 1200 → trade at 1000 (seller's price)
        await self._submit_order(client, seller_key, limit_order("ask", 1000, 100, "EXEC1"))
        buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1200, 100, "EXEC1"))

        ok = await poll(lambda: self._check_order_traded(client, buyer_key, buyer_oid))
        assert ok, "expected trade to occur"

        order = await self._get_order(client, buyer_key, buyer_oid)
        trade_price = order["trades"][0]["price"]
        assert trade_price == 1000, f"trade price {trade_price}, expected 1000 (seller's price)"

        # Reverse: seller at 1200, buyer at 1000 → no match
        await self._submit_order(client, seller_key, limit_order("ask", 1200, 100, "EXEC2"))
        buyer_oid2 = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 100, "EXEC2"))
        await asyncio.sleep(0.2)
        order2 = await self._get_order(client, buyer_key, buyer_oid2)
        assert len(order2.get("trades", [])) == 0, "expected no trade for reverse gap"

    # ------------------------------------------------------------------
    # Test 3: Price-time priority
    # ------------------------------------------------------------------
    async def test_price_time_priority(self, client: httpx.AsyncClient):
        key_a = await self._register_broker(client, "SellerA")
        key_b = await self._register_broker(client, "SellerB")
        key_c = await self._register_broker(client, "SellerC")
        buyer_key = await self._register_broker(client, "Buyer")

        # A, B, C all ask at 1000 — A is earliest
        oid_a = await self._submit_order(client, key_a, limit_order("ask", 1000, 100, "PTP1"))
        await asyncio.sleep(0.05)
        oid_b = await self._submit_order(client, key_b, limit_order("ask", 1000, 100, "PTP1"))
        await asyncio.sleep(0.05)
        oid_c = await self._submit_order(client, key_c, limit_order("ask", 1000, 100, "PTP1"))

        # Buyer matches one
        buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 100, "PTP1"))
        ok = await poll(lambda: self._check_order_traded(client, buyer_key, buyer_oid))
        assert ok, "expected trade"

        # A should be matched (closed), B and C still open
        order_a = await self._get_order(client, key_a, oid_a)
        assert order_a["status"] == "closed", f"A should be closed, got {order_a['status']}"

        order_b = await self._get_order(client, key_b, oid_b)
        assert order_b["status"] == "open", f"B should be open, got {order_b['status']}"

        order_c = await self._get_order(client, key_c, oid_c)
        assert order_c["status"] == "open", f"C should be open, got {order_c['status']}"

    # ------------------------------------------------------------------
    # Test 4: Partial fill — single
    # ------------------------------------------------------------------
    async def test_partial_fill_single(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_key = await self._register_broker(client, "Buyer")

        seller_oid = await self._submit_order(client, seller_key, limit_order("ask", 1000, 1000, "PFS1"))
        buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 300, "PFS1"))

        ok = await poll(lambda: self._check_order_traded(client, buyer_key, buyer_oid))
        assert ok, "expected trade"

        buyer_order = await self._get_order(client, buyer_key, buyer_oid)
        assert len(buyer_order["trades"]) == 1, f"expected 1 trade, got {len(buyer_order['trades'])}"
        assert buyer_order["trades"][0]["quantity"] == 300
        assert buyer_order["status"] == "closed"

        seller_order = await self._get_order(client, seller_key, seller_oid)
        assert seller_order["remaining_quantity"] == 700, f"seller remaining {seller_order['remaining_quantity']}, expected 700"
        assert seller_order["status"] == "open"

    # ------------------------------------------------------------------
    # Test 5: Partial fill — multi
    # ------------------------------------------------------------------
    async def test_partial_fill_multi(self, client: httpx.AsyncClient):
        sellers = []
        for i in range(5):
            key = await self._register_broker(client, f"Seller{i}")
            oid = await self._submit_order(client, key, limit_order("ask", 1000, 100, "PFM1"))
            sellers.append((key, oid))

        buyer_key = await self._register_broker(client, "Buyer")
        buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 500, "PFM1"))

        ok = await poll(lambda: self._check_trades_count(client, buyer_key, buyer_oid, 5))
        assert ok, "expected 5 trades"

        buyer_order = await self._get_order(client, buyer_key, buyer_oid)
        assert buyer_order["status"] == "closed"
        assert len(buyer_order["trades"]) == 5
        for t in buyer_order["trades"]:
            assert t["price"] == 1000

        for key, oid in sellers:
            order = await self._get_order(client, key, oid)
            assert order["status"] == "closed", f"seller {oid} should be closed"

    async def _check_trades_count(self, client, key, order_id, expected) -> bool:
        try:
            order = await self._get_order(client, key, order_id)
            return len(order.get("trades", [])) >= expected
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Test 6: No match
    # ------------------------------------------------------------------
    async def test_no_match(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_key = await self._register_broker(client, "Buyer")

        seller_oid = await self._submit_order(client, seller_key, limit_order("ask", 2000, 100, "NOM1"))
        buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 100, "NOM1"))

        await asyncio.sleep(0.2)

        seller_order = await self._get_order(client, seller_key, seller_oid)
        assert seller_order["status"] == "open"
        assert seller_order["remaining_quantity"] == 100
        assert len(seller_order.get("trades", [])) == 0

        buyer_order = await self._get_order(client, buyer_key, buyer_oid)
        assert buyer_order["status"] == "open"
        assert buyer_order["remaining_quantity"] == 100
        assert len(buyer_order.get("trades", [])) == 0

    # ------------------------------------------------------------------
    # Test 7: Expiration
    # ------------------------------------------------------------------
    async def test_expiration(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_key = await self._register_broker(client, "Buyer")

        # Sell with valid_until = now + 1 second
        order_body = limit_order("ask", 1000, 100, "EXP1")
        order_body["valid_until"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        seller_oid = await self._submit_order(client, seller_key, order_body)

        # Wait for expiration
        await asyncio.sleep(2.5)

        # Submit matching buy — should NOT match the expired order
        buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 100, "EXP1"))
        await asyncio.sleep(0.3)

        buyer_order = await self._get_order(client, buyer_key, buyer_oid)
        assert len(buyer_order.get("trades", [])) == 0, "expected no trade with expired order"

    # ------------------------------------------------------------------
    # Test 8: Market order — match
    # ------------------------------------------------------------------
    async def test_market_order_match(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_key = await self._register_broker(client, "Buyer")

        await self._submit_order(client, seller_key, limit_order("ask", 1000, 100, "MKT1"))
        buyer_oid = await self._submit_order(client, buyer_key, market_order("bid", 100, "MKT1"))

        ok = await poll(lambda: self._check_order_traded(client, buyer_key, buyer_oid))
        assert ok, "expected market order to match"

        buyer_order = await self._get_order(client, buyer_key, buyer_oid)
        assert buyer_order["trades"][0]["price"] == 1000
        assert buyer_order["status"] == "closed"

    # ------------------------------------------------------------------
    # Test 9: Market order — IOC cancel
    # ------------------------------------------------------------------
    async def test_market_order_ioc_cancel(self, client: httpx.AsyncClient):
        buyer_key = await self._register_broker(client, "Buyer")

        # No liquidity
        buyer_oid = await self._submit_order(client, buyer_key, market_order("bid", 100, "IOC1"))

        async def check_closed():
            try:
                order = await self._get_order(client, buyer_key, buyer_oid)
                return order["status"] == "closed"
            except Exception:
                return False

        ok = await poll(check_closed)
        assert ok, "market order should be closed (IOC)"

        order = await self._get_order(client, buyer_key, buyer_oid)
        assert order["remaining_quantity"] == 100, f"remaining should be 100, got {order['remaining_quantity']}"

    # ------------------------------------------------------------------
    # Test 10: Market order — partial IOC
    # ------------------------------------------------------------------
    async def test_market_order_partial_ioc(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_key = await self._register_broker(client, "Buyer")

        await self._submit_order(client, seller_key, limit_order("ask", 1000, 50, "PIOC"))
        buyer_oid = await self._submit_order(client, buyer_key, market_order("bid", 100, "PIOC"))

        ok = await poll(lambda: self._check_order_traded(client, buyer_key, buyer_oid))
        assert ok, "expected partial fill"

        buyer_order = await self._get_order(client, buyer_key, buyer_oid)
        assert len(buyer_order["trades"]) == 1
        assert buyer_order["trades"][0]["quantity"] == 50
        assert buyer_order["status"] == "closed", "market order should be closed after partial fill (IOC)"
        assert buyer_order["remaining_quantity"] == 50, f"remaining should be 50, got {buyer_order['remaining_quantity']}"

    # ------------------------------------------------------------------
    # Test 11: Order book state
    # ------------------------------------------------------------------
    async def test_order_book_state(self, client: httpx.AsyncClient):
        key = await self._register_broker(client, "BookTest")

        # Submit orders at different price levels
        await self._submit_order(client, key, limit_order("ask", 1100, 100, "BOOK"))
        await self._submit_order(client, key, limit_order("ask", 1100, 200, "BOOK"))
        await self._submit_order(client, key, limit_order("ask", 1200, 50, "BOOK"))
        await self._submit_order(client, key, limit_order("bid", 900, 150, "BOOK"))
        await self._submit_order(client, key, limit_order("bid", 900, 100, "BOOK"))
        await self._submit_order(client, key, limit_order("bid", 800, 300, "BOOK"))

        async def check_book():
            try:
                book = await self._get_book(client, key, "BOOK")
                asks = book.get("asks", [])
                bids = book.get("bids", [])
                if len(asks) < 2 or len(bids) < 2:
                    return False
                return True
            except Exception:
                return False

        ok = await poll(check_book)
        assert ok, "book not populated"

        book = await self._get_book(client, key, "BOOK")
        asks = book["asks"]
        bids = book["bids"]

        # Asks sorted by price ascending
        ask_1100 = next((a for a in asks if a["price"] == 1100), None)
        assert ask_1100 is not None, "missing ask at 1100"
        assert ask_1100["total_quantity"] == 300, f"ask@1100 qty={ask_1100['total_quantity']}, expected 300"
        assert ask_1100["order_count"] == 2

        ask_1200 = next((a for a in asks if a["price"] == 1200), None)
        assert ask_1200 is not None
        assert ask_1200["total_quantity"] == 50

        # Bids sorted by price descending
        bid_900 = next((b for b in bids if b["price"] == 900), None)
        assert bid_900 is not None
        assert bid_900["total_quantity"] == 250, f"bid@900 qty={bid_900['total_quantity']}, expected 250"
        assert bid_900["order_count"] == 2

        bid_800 = next((b for b in bids if b["price"] == 800), None)
        assert bid_800 is not None
        assert bid_800["total_quantity"] == 300

    # ------------------------------------------------------------------
    # Test 12: Concurrent contention
    # ------------------------------------------------------------------
    async def test_concurrent_contention(self, client: httpx.AsyncClient):
        seller_key = await self._register_broker(client, "Seller")
        buyer_keys = []
        for i in range(10):
            buyer_keys.append(await self._register_broker(client, f"Buyer{i}"))

        # One sell order qty=100
        await self._submit_order(client, seller_key, limit_order("ask", 1000, 100, "CONT"))

        # 10 concurrent buy orders qty=100 each
        async def submit_buy(key):
            return await self._submit_order(client, key, limit_order("bid", 1000, 100, "CONT"))

        buyer_oids = await asyncio.gather(*[submit_buy(k) for k in buyer_keys])

        # Wait for matching to settle
        await asyncio.sleep(1.0)

        # Count trades across all buyers
        total_trades = 0
        matched_buyers = 0
        for key, oid in zip(buyer_keys, buyer_oids):
            order = await self._get_order(client, key, oid)
            trades = order.get("trades", [])
            total_trades += len(trades)
            if len(trades) > 0:
                matched_buyers += 1

        assert total_trades == 1, f"expected exactly 1 trade, got {total_trades}"
        assert matched_buyers == 1, f"expected exactly 1 matched buyer, got {matched_buyers}"

    # ------------------------------------------------------------------
    # Test 13: Webhook delivery
    # ------------------------------------------------------------------
    async def test_webhook_delivery(self, client: httpx.AsyncClient):
        sink = WebhookSink(store_payloads=True)
        sink.start()
        try:
            seller_key = await self._register_broker(client, "WebhookSeller", webhook_url=sink.url)
            buyer_key = await self._register_broker(client, "WebhookBuyer", webhook_url=sink.url)

            await self._submit_order(client, seller_key, limit_order("ask", 1000, 100, "WHK1"))
            buyer_oid = await self._submit_order(client, buyer_key, limit_order("bid", 1000, 100, "WHK1"))

            ok = await poll(lambda: self._check_order_traded(client, buyer_key, buyer_oid))
            assert ok, "expected trade"

            # Wait for webhooks
            webhook_ok = await poll(lambda: self._check_webhook_count(sink, 2), timeout=3.0)
            assert webhook_ok, f"expected 2 webhooks, got {sink.count}"

            payloads = sink.payloads
            assert len(payloads) == 2, f"expected 2 payloads, got {len(payloads)}"

            # Both should have trade info
            for p in payloads:
                assert "trade_id" in p, f"missing trade_id in webhook: {p}"
                assert "order_id" in p, f"missing order_id in webhook: {p}"
                assert p["symbol"] == "WHK1", f"wrong symbol: {p['symbol']}"
                assert p["price"] == 1000, f"wrong price: {p['price']}"
                assert p["quantity"] == 100, f"wrong quantity: {p['quantity']}"
                assert p["side"] in ("bid", "ask"), f"wrong side: {p['side']}"

            # One for buyer side, one for seller side
            sides = {p["side"] for p in payloads}
            assert sides == {"bid", "ask"}, f"expected both sides, got {sides}"
        finally:
            sink.stop()

    async def _check_webhook_count(self, sink, expected) -> bool:
        return sink.count >= expected

    # ------------------------------------------------------------------
    # Test 14: Cancel order
    # ------------------------------------------------------------------
    async def test_cancel_order(self, client: httpx.AsyncClient):
        key_a = await self._register_broker(client, "CancelSeller")
        key_b = await self._register_broker(client, "CancelBuyer")

        # Broker A submits a limit ask
        ask_oid = await self._submit_order(client, key_a, limit_order("ask", 1000, 100, "CANC"))

        # Cancel it
        resp = await client.post(f"/orders/{ask_oid}/cancel", headers=broker_header(key_a))
        assert resp.status_code == 204, f"cancel failed: {resp.status_code} {resp.text}"

        # Broker B submits a matching bid — should NOT match the cancelled ask
        bid_oid = await self._submit_order(client, key_b, limit_order("bid", 1000, 100, "CANC"))

        # Wait until bid is readable, then verify no trades
        async def check_bid_readable():
            try:
                await self._get_order(client, key_b, bid_oid)
                return True
            except Exception:
                return False

        ok = await poll(check_bid_readable)
        assert ok, "bid order not readable"

        bid_order = await self._get_order(client, key_b, bid_oid)
        assert len(bid_order.get("trades", [])) == 0, "cancelled ask should not match"
        assert bid_order["status"] == "open", f"bid should be open, got {bid_order['status']}"

        # Verify cancelled order is closed
        ask_order = await self._get_order(client, key_a, ask_oid)
        assert ask_order["status"] == "closed", f"cancelled order should be closed, got {ask_order['status']}"

    # ------------------------------------------------------------------
    # Test 15: Stock price
    # ------------------------------------------------------------------
    async def test_stock_price(self, client: httpx.AsyncClient):
        key_a = await self._register_broker(client, "PriceSeller")
        key_b = await self._register_broker(client, "PriceBuyer")

        # Create a trade at price 1000
        await self._submit_order(client, key_a, limit_order("ask", 1000, 100, "SPR1"))
        bid_oid = await self._submit_order(client, key_b, limit_order("bid", 1000, 100, "SPR1"))

        ok = await poll(lambda: self._check_order_traded(client, key_b, bid_oid))
        assert ok, "expected trade to complete"

        # Query stock price
        resp = await client.get("/stocks/SPR1/price", headers=broker_header(key_a))
        assert resp.status_code == 200, f"stock price failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data["last_price"] == 1000, f"last_price={data['last_price']}, expected 1000"
        assert data["trades_in_average"] >= 1, f"trades_in_average={data['trades_in_average']}, expected >= 1"

        # 404 for symbol with no trades
        resp = await client.get("/stocks/NOEXIST/price", headers=broker_header(key_a))
        assert resp.status_code == 404, f"expected 404 for unknown symbol, got {resp.status_code}"


async def main():
    parser = argparse.ArgumentParser(description="Correctness tests for the mini stock exchange")
    parser.add_argument("--version", required=True, help="Version label (e.g. v1, v2)")
    parser.add_argument("--url", default=DEFAULT_URL, help="API base URL")
    parser.add_argument("--admin-key", default=DEFAULT_ADMIN_KEY, help="Admin API key")
    args = parser.parse_args()

    runner = CorrectnessRunner(args.url, args.admin_key, args.version)
    passed, failed = await runner.run_all()

    # Save results
    data = {
        "type": "correctness",
        "version": args.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "tests": runner.results,
        "passed": passed,
        "failed": failed,
    }
    save_results(data, "correctness")


if __name__ == "__main__":
    asyncio.run(main())
