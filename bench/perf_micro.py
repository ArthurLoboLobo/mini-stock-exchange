"""
Micro-benchmarks for the mini stock exchange.

Isolated operation costs and scaling curves. Each scenario resets the DB,
seeds its own state, and measures one thing.

Usage:
    python perf_micro.py --version v1
    python perf_micro.py --version v1 --scenario book_depth
    python perf_micro.py --version v1 --iterations 500 --symbols 500
"""
import argparse
import asyncio
import random
import time
from datetime import datetime, timezone

import httpx

from shared import (
    DEFAULT_ADMIN_KEY,
    DEFAULT_URL,
    BOLD,
    CYAN,
    RESET,
    BenchmarkResult,
    admin_headers,
    broker_header,
    generate_base_prices,
    generate_symbols,
    limit_order,
    market_order,
    random_broker_header,
    register_brokers,
    reset_db,
    save_results,
    timed_request,
)


SCENARIO_DISPLAY_NAMES = {
    "submit_no_match": "Passive Limit",
    "submit_with_match": "Aggressive Limit",
    "submit_market_order": "Market Order",
    "get_order_status": "Get Order",
    "query_order_book": "Order Book",
    "query_stock_price": "Stock Price",
    "query_balance": "Balance",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_book(
    client: httpx.AsyncClient,
    keys: list[str],
    symbols: list[str],
    base_prices: dict[str, int],
    rng: random.Random,
    orders_per_side: int = 20,
):
    sem = asyncio.Semaphore(50)
    count = 0

    async def place(body, key):
        nonlocal count
        async with sem:
            await client.post("/orders", headers=broker_header(key), json=body)
            count += 1

    tasks = []
    for symbol in symbols:
        bp = base_prices[symbol]
        for i in range(orders_per_side):
            ask_price = bp + max(bp * (1 + i * 9 // max(orders_per_side - 1, 1)) // 100, 1)
            bid_price = bp - max(bp * (1 + i * 9 // max(orders_per_side - 1, 1)) // 100, 1)
            tasks.append(place(limit_order("ask", ask_price, 500, symbol, rng), rng.choice(keys)))
            tasks.append(place(limit_order("bid", max(bid_price, 1), 500, symbol, rng), rng.choice(keys)))

    await asyncio.gather(*tasks)
    return count


async def _run_burst(
    client: httpx.AsyncClient,
    bench: BenchmarkResult,
    iterations: int,
    concurrency: int,
    keys: list[str],
    symbols: list[str],
    rng: random.Random,
    gen_fn,
):
    sem = asyncio.Semaphore(concurrency)

    async def one():
        async with sem:
            method, url, headers, body, expect = gen_fn()
            lat, resp = await timed_request(client, method, url, headers, body)
            if resp and resp.status_code in expect:
                bench.record(lat)
            else:
                bench.record_error()

    tasks = [asyncio.create_task(one()) for _ in range(iterations)]
    done, pending = await asyncio.wait(tasks, timeout=60.0)
    for t in pending:
        t.cancel()


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------

async def warm_up(client: httpx.AsyncClient, admin_key: str, rng: random.Random):
    print(f"\n{BOLD}Warm-up phase{RESET} (200 mixed ops, discarded)...")
    await reset_db(client, admin_key)

    warmup_symbols = ["WARM1", "WARM2", "WARM3"]
    keys = await register_brokers(client, 5, admin_key)
    if not keys:
        return

    base_prices = {s: 1000 for s in warmup_symbols}
    await _seed_book(client, keys, warmup_symbols, base_prices, rng, orders_per_side=10)

    sem = asyncio.Semaphore(20)

    async def fire(method, url, headers, body=None):
        async with sem:
            try:
                await client.request(method, url, headers=headers, json=body)
            except Exception:
                pass

    tasks = []
    for _ in range(100):
        sym = rng.choice(warmup_symbols)
        key = rng.choice(keys)
        h = broker_header(key)
        tasks.append(fire("POST", "/orders", h, limit_order("bid", 500, 100, sym, rng)))
        tasks.append(fire("GET", f"/stocks/{sym}/book", h))

    await asyncio.gather(*tasks)
    print("  Warm-up complete (results discarded)\n")


# ---------------------------------------------------------------------------
# Scenario group 1: Single operation latencies
# ---------------------------------------------------------------------------

async def run_latencies(
    client: httpx.AsyncClient,
    admin_key: str,
    keys: list[str],
    symbols: list[str],
    base_prices: dict[str, int],
    iterations: int,
    concurrency: int,
    rng: random.Random,
) -> list[dict]:
    results = []

    # --- submit_no_match ---
    print(f"  {CYAN}Passive Limit{RESET} ({iterations} iters)...", end=" ", flush=True)
    await reset_db(client, admin_key)
    run_keys = await register_brokers(client, len(keys), admin_key)
    await _seed_book(client, run_keys, symbols, base_prices, rng)
    bench = BenchmarkResult("submit_no_match")

    def gen_no_match():
        sym = rng.choice(symbols)
        return "POST", "/orders", random_broker_header(run_keys, rng), limit_order("bid", 1, 100, sym, rng), {201}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_no_match)
    results.append(bench.summary())
    print("done")

    # --- submit_with_match ---
    print(f"  {CYAN}Aggressive Limit{RESET} ({iterations} iters)...", end=" ", flush=True)
    await reset_db(client, admin_key)
    run_keys = await register_brokers(client, len(keys), admin_key)
    await _seed_book(client, run_keys, symbols, base_prices, rng)
    # Seed extra asks at base price for matching
    sem = asyncio.Semaphore(50)

    async def seed_ask(sym, price, key):
        async with sem:
            await client.post("/orders", headers=broker_header(key), json=limit_order("ask", price, 100, sym, rng))

    seed_tasks = []
    for sym in symbols:
        bp = base_prices[sym]
        for _ in range(iterations // len(symbols) + 10):
            seed_tasks.append(seed_ask(sym, bp, rng.choice(run_keys)))
    await asyncio.gather(*seed_tasks)

    bench = BenchmarkResult("submit_with_match")

    def gen_match():
        sym = rng.choice(symbols)
        bp = base_prices[sym]
        return "POST", "/orders", random_broker_header(run_keys, rng), limit_order("bid", bp, 100, sym, rng), {201}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_match)
    results.append(bench.summary())
    print("done")

    # --- submit_market_order ---
    print(f"  {CYAN}Market Order{RESET} ({iterations} iters)...", end=" ", flush=True)
    await reset_db(client, admin_key)
    run_keys = await register_brokers(client, len(keys), admin_key)
    await _seed_book(client, run_keys, symbols, base_prices, rng)
    # Seed asks at base price
    seed_tasks = []
    for sym in symbols:
        bp = base_prices[sym]
        for _ in range(iterations // len(symbols) + 10):
            seed_tasks.append(seed_ask(sym, bp, rng.choice(run_keys)))
    await asyncio.gather(*seed_tasks)

    bench = BenchmarkResult("submit_market_order")

    def gen_market():
        sym = rng.choice(symbols)
        return "POST", "/orders", random_broker_header(run_keys, rng), market_order("bid", 50, sym, rng), {201}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_market)
    results.append(bench.summary())
    print("done")

    # --- get_order_status ---
    print(f"  {CYAN}Get Order{RESET} ({iterations} iters)...", end=" ", flush=True)
    await reset_db(client, admin_key)
    run_keys = await register_brokers(client, len(keys), admin_key)
    await _seed_book(client, run_keys, symbols, base_prices, rng)
    # Create one order per broker for reads
    order_ids_by_key = {}
    for key in run_keys:
        sym = rng.choice(symbols)
        resp = await client.post("/orders", headers=broker_header(key), json=limit_order("bid", 1, 100, sym, rng))
        if resp.status_code == 201:
            order_ids_by_key[key] = resp.json()["order_id"]

    bench = BenchmarkResult("get_order_status")

    def gen_get_order():
        key = rng.choice(run_keys)
        oid = order_ids_by_key.get(key, "")
        return "GET", f"/orders/{oid}", broker_header(key), None, {200}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_get_order)
    results.append(bench.summary())
    print("done")

    # --- query_order_book ---
    print(f"  {CYAN}Order Book{RESET} ({iterations} iters)...", end=" ", flush=True)
    bench = BenchmarkResult("query_order_book")
    idx = [0]

    def gen_book():
        sym = symbols[idx[0] % len(symbols)]
        idx[0] += 1
        return "GET", f"/stocks/{sym}/book", random_broker_header(run_keys, rng), None, {200}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_book)
    results.append(bench.summary())
    print("done")

    # --- query_stock_price ---
    print(f"  {CYAN}Stock Price{RESET} ({iterations} iters)...", end=" ", flush=True)
    bench = BenchmarkResult("query_stock_price")
    idx[0] = 0

    def gen_price():
        sym = symbols[idx[0] % len(symbols)]
        idx[0] += 1
        return "GET", f"/stocks/{sym}/price", random_broker_header(run_keys, rng), None, {200, 404}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_price)
    results.append(bench.summary())
    print("done")

    # --- query_balance ---
    print(f"  {CYAN}Balance{RESET} ({iterations} iters)...", end=" ", flush=True)
    bench = BenchmarkResult("query_balance")

    def gen_balance():
        return "GET", "/balance", random_broker_header(run_keys, rng), None, {200}

    await _run_burst(client, bench, iterations, concurrency, run_keys, symbols, rng, gen_balance)
    results.append(bench.summary())
    print("done")

    return results


# ---------------------------------------------------------------------------
# Scenario group 2: Book depth scaling
# ---------------------------------------------------------------------------

async def run_book_depth(
    client: httpx.AsyncClient,
    admin_key: str,
    num_brokers: int,
    rng: random.Random,
) -> list[dict]:
    depths = [100, 500, 1000, 5000, 10000]
    measurements = 50
    results = []
    symbol = "DEPTH"

    print(f"\n  {BOLD}Book Depth Scaling{RESET} (match latency)")

    for depth in depths:
        print(f"  depth={depth}...", end=" ", flush=True)
        await reset_db(client, admin_key)
        run_keys = await register_brokers(client, num_brokers, admin_key)

        # Seed N asks
        sem = asyncio.Semaphore(50)

        async def place_ask(price, key):
            async with sem:
                await client.post("/orders", headers=broker_header(key), json=limit_order("ask", price, 100, symbol, rng))

        tasks = [place_ask(1000 + i, rng.choice(run_keys)) for i in range(depth)]
        await asyncio.gather(*tasks)

        # Measure match latency
        bench = BenchmarkResult(f"depth_{depth}")
        for _ in range(measurements):
            # Seed one ask at the best price to match against
            await client.post(
                "/orders",
                headers=broker_header(rng.choice(run_keys)),
                json=limit_order("ask", 999, 10, symbol, rng),
            )
            lat, resp = await timed_request(
                client, "POST", "/orders",
                random_broker_header(run_keys, rng),
                limit_order("bid", 999, 10, symbol, rng),
            )
            if resp and resp.status_code == 201:
                bench.record(lat)
            else:
                bench.record_error()

        s = bench.summary()
        results.append({"depth": depth, **s})
        print(f"p50={s.get('p50_ms', 0):.1f}ms  p99={s.get('p99_ms', 0):.1f}ms")

    return results


# ---------------------------------------------------------------------------
# Scenario group 3: Partial fill depth
# ---------------------------------------------------------------------------

async def run_partial_fill(
    client: httpx.AsyncClient,
    admin_key: str,
    num_brokers: int,
    rng: random.Random,
) -> list[dict]:
    fill_depths = [1, 5, 10, 50, 100]
    repetitions = 20
    results = []
    symbol = "PFILL"

    print(f"\n  {BOLD}Partial Fill Depth{RESET} (N matches per order)")

    for n in fill_depths:
        print(f"  fills={n}...", end=" ", flush=True)
        bench = BenchmarkResult(f"fills_{n}")

        for _ in range(repetitions):
            await reset_db(client, admin_key)
            run_keys = await register_brokers(client, num_brokers, admin_key)

            # Seed N asks qty=10 each
            sem = asyncio.Semaphore(50)

            async def place_ask(key):
                async with sem:
                    await client.post("/orders", headers=broker_header(key), json=limit_order("ask", 1000, 10, symbol, rng))

            tasks = [place_ask(rng.choice(run_keys)) for _ in range(n)]
            await asyncio.gather(*tasks)

            # One bid qty = N * 10
            lat, resp = await timed_request(
                client, "POST", "/orders",
                random_broker_header(run_keys, rng),
                limit_order("bid", 1000, n * 10, symbol, rng),
            )
            if resp and resp.status_code == 201:
                bench.record(lat)
            else:
                bench.record_error()

        s = bench.summary()
        results.append({"fills": n, **s})
        print(f"p50={s.get('p50_ms', 0):.1f}ms  p99={s.get('p99_ms', 0):.1f}ms")

    return results


# ---------------------------------------------------------------------------
# Scenario group 4: Concurrency sweep
# ---------------------------------------------------------------------------

async def run_concurrency_sweep(
    client: httpx.AsyncClient,
    admin_key: str,
    symbols: list[str],
    base_prices: dict[str, int],
    num_brokers: int,
    iterations: int,
    rng: random.Random,
) -> list[dict]:
    levels = [1, 5, 10, 25, 50, 100]
    results = []

    print(f"\n  {BOLD}Concurrency Sweep{RESET} (submit_with_match)")

    for c in levels:
        print(f"  concurrency={c}...", end=" ", flush=True)
        await reset_db(client, admin_key)
        run_keys = await register_brokers(client, num_brokers, admin_key)
        await _seed_book(client, run_keys, symbols, base_prices, rng)

        # Seed asks at base price
        sem = asyncio.Semaphore(50)

        async def seed_ask(sym, price, key):
            async with sem:
                await client.post("/orders", headers=broker_header(key), json=limit_order("ask", price, 100, sym, rng))

        seed_tasks = []
        for sym in symbols:
            bp = base_prices[sym]
            for _ in range(iterations // len(symbols) + 10):
                seed_tasks.append(seed_ask(sym, bp, rng.choice(run_keys)))
        await asyncio.gather(*seed_tasks)

        bench = BenchmarkResult(f"concurrency_{c}")

        def gen_match():
            sym = rng.choice(symbols)
            bp = base_prices[sym]
            return "POST", "/orders", random_broker_header(run_keys, rng), limit_order("bid", bp, 100, sym, rng), {201}

        await _run_burst(client, bench, iterations, c, run_keys, symbols, rng, gen_match)
        s = bench.summary()
        results.append({"concurrency": c, **s})
        print(f"p50={s.get('p50_ms', 0):.1f}ms  p99={s.get('p99_ms', 0):.1f}ms")

    return results


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _print_bench(bench: BenchmarkResult):
    s = bench.summary()
    if s.get("count", 0) > 0:
        print(f"    p50={s['p50_ms']:.1f}ms  p95={s['p95_ms']:.1f}ms  p99={s['p99_ms']:.1f}ms  max={s['max_ms']:.1f}ms  errors={s['errors']}")
    else:
        print(f"    no data (errors={s['errors']})")


def print_summary(latencies, book_depth, partial_fill, concurrency_data):
    print(f"\n{'=' * 60}")
    print(f"{BOLD}SUMMARY{RESET}")
    print(f"{'=' * 60}")

    if latencies:
        print(f"\n{'Scenario':<25} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'errors':>8}")
        print("-" * 68)
        for s in latencies:
            name = SCENARIO_DISPLAY_NAMES.get(s['name'], s['name'])
            if s.get("count", 0) > 0:
                print(f"{name:<25} {s['p50_ms']:>7.1f}ms {s['p95_ms']:>7.1f}ms {s['p99_ms']:>7.1f}ms {s['max_ms']:>7.1f}ms {s['errors']:>8}")
            else:
                print(f"{name:<25} {'—':>8} {'—':>8} {'—':>8} {'—':>8} {s['errors']:>8}")

    if book_depth:
        print(f"\nBook Depth Scaling:")
        for s in book_depth:
            if s.get("count", 0) > 0:
                print(f"  depth={s['depth']:<6}  p50={s['p50_ms']:.1f}ms  p99={s['p99_ms']:.1f}ms")

    if partial_fill:
        print(f"\nPartial Fill Depth:")
        for s in partial_fill:
            if s.get("count", 0) > 0:
                print(f"  fills={s['fills']:<6}  p50={s['p50_ms']:.1f}ms  p99={s['p99_ms']:.1f}ms")

    if concurrency_data:
        print(f"\nConcurrency Sweep:")
        for s in concurrency_data:
            if s.get("count", 0) > 0:
                print(f"  c={s['concurrency']:<6}  p50={s['p50_ms']:.1f}ms  p99={s['p99_ms']:.1f}ms")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Micro-benchmarks for the mini stock exchange")
    parser.add_argument("--version", required=True, help="Version label (e.g., v1, v2)")
    parser.add_argument("--url", default=DEFAULT_URL, help="API base URL")
    parser.add_argument("--admin-key", default=DEFAULT_ADMIN_KEY, help="Admin API key")
    parser.add_argument("--scenario", default=None, choices=["latencies", "book_depth", "partial_fill", "concurrency"],
                        help="Run only a specific scenario group")
    parser.add_argument("--iterations", type=int, default=200, help="Requests per measurement point")
    parser.add_argument("--concurrency", type=int, default=10, help="Max concurrent requests")
    parser.add_argument("--symbols", type=int, default=50, help="Number of stock symbols")
    parser.add_argument("--brokers", type=int, default=20, help="Number of brokers")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    symbols = generate_symbols(args.symbols)
    base_prices = generate_base_prices(symbols, rng)

    print(f"\n{BOLD}Mini Stock Exchange — Micro-Benchmarks{RESET}")
    print(f"  Version:     {args.version}")
    print(f"  URL:         {args.url}")
    print(f"  Symbols:     {args.symbols}")
    print(f"  Brokers:     {args.brokers}")
    print(f"  Iterations:  {args.iterations}")
    print(f"  Concurrency: {args.concurrency}")

    latencies = []
    book_depth = []
    partial_fill = []
    concurrency_data = []

    async with httpx.AsyncClient(base_url=args.url, timeout=30.0) as client:
        await warm_up(client, args.admin_key, rng)

        run_all = args.scenario is None

        if run_all or args.scenario == "latencies":
            print(f"\n{BOLD}Single Operation Latencies{RESET}")
            latencies = await run_latencies(
                client, args.admin_key, [""] * args.brokers, symbols, base_prices,
                args.iterations, args.concurrency, rng,
            )

        if run_all or args.scenario == "book_depth":
            book_depth = await run_book_depth(client, args.admin_key, args.brokers, rng)

        if run_all or args.scenario == "partial_fill":
            partial_fill = await run_partial_fill(client, args.admin_key, args.brokers, rng)

        if run_all or args.scenario == "concurrency":
            concurrency_data = await run_concurrency_sweep(
                client, args.admin_key, symbols, base_prices,
                args.brokers, args.iterations, rng,
            )

    print_summary(latencies, book_depth, partial_fill, concurrency_data)

    output = {
        "type": "micro",
        "version": args.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "symbols": args.symbols,
            "brokers": args.brokers,
            "iterations": args.iterations,
            "concurrency": args.concurrency,
            "seed": args.seed,
        },
        "latencies": latencies,
        "book_depth": book_depth,
        "partial_fill": partial_fill,
        "concurrency": concurrency_data,
    }
    save_results(output, f"{args.version}_micro")


if __name__ == "__main__":
    asyncio.run(main())
