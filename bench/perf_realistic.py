"""
Realistic benchmark for the mini stock exchange.

Simulates a window of a real trading day with
Poisson arrivals, coordinated omission correction, and time-series output.

Uses aiohttp + uvloop for high-throughput HTTP dispatch.
Supports multiprocessing workers to scale beyond single-process limits.

Usage:
    python perf_realistic.py --version v1
    python perf_realistic.py --version v1 --scale 100 --duration 120
    python perf_realistic.py --version v2 --scale 25 --duration 10 --workers 1
"""
import argparse
import asyncio
import math
import multiprocessing
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp

try:
    import uvloop
except ImportError:
    uvloop = None

from shared import (
    DEFAULT_ADMIN_KEY,
    DEFAULT_URL,
    BOLD,
    CYAN,
    GREEN,
    RED,
    RESET,
    BenchmarkResult,
    admin_headers,
    broker_header,
    generate_base_prices,
    generate_symbols,
    limit_order,
    market_order,
    save_results,
    zipf_weights,
)


# ---------------------------------------------------------------------------
# Display name mapping
# ---------------------------------------------------------------------------

CATEGORY_DISPLAY_NAMES = {
    "write_no_match": "Passive Limit",
    "write_match": "Aggressive Limit",
    "write_market": "Market Order",
    "cancel": "Cancel",
    "read": "Read",
    "overall": "Overall",
}


# ---------------------------------------------------------------------------
# B3 reference values (100% scale)
# ---------------------------------------------------------------------------

B3_SYMBOLS = 450
B3_BROKERS = 100
ZIPF_EXPONENT = 0.95
B3_ORDER_RATE = 700
B3_CANCEL_RATE = 245
READ_MIX = 0.28
PASSIVE_LIMIT_PCT = 0.75
AGGRESSIVE_LIMIT_PCT = 0.20
MARKET_ORDER_PCT = 0.05
CANCEL_FAST_PCT = 0.40
CANCEL_MEDIUM_PCT = 0.35
CANCEL_SLOW_PCT = 0.25
READ_ORDER_STATUS_PCT = 0.35
READ_PRICE_PCT = 0.30
READ_BOOK_PCT = 0.25
READ_BALANCE_PCT = 0.10
DURATION_VERY_SHORT_PCT = 0.10
DURATION_SHORT_PCT = 0.20
DURATION_DAY_PCT = 0.70


def scale_params(scale_pct: int) -> dict:
    rate_factor = scale_pct / 100.0
    structure_factor = min(scale_pct, 100) / 100.0
    return {
        "symbols": max(int(B3_SYMBOLS * structure_factor), 5),
        "brokers": max(int(B3_BROKERS * structure_factor), 3),
        "order_rate": max(int(B3_ORDER_RATE * rate_factor), 10),
        "cancel_rate": max(int(B3_CANCEL_RATE * rate_factor), 5),
    }


# ---------------------------------------------------------------------------
# aiohttp session factory
# ---------------------------------------------------------------------------

def make_session(url: str) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        limit=0,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    timeout = aiohttp.ClientTimeout(total=30)
    return aiohttp.ClientSession(
        base_url=url,
        connector=connector,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# aiohttp-native variants of shared helpers (avoids modifying shared.py)
# ---------------------------------------------------------------------------

async def _register_brokers(
    session: aiohttp.ClientSession,
    count: int,
    admin_key: str = DEFAULT_ADMIN_KEY,
) -> list[str]:
    keys = []
    for i in range(count):
        body = {"name": f"Bench Broker {i + 1}"}
        async with session.post(
            "/register", json=body, headers=admin_headers(admin_key)
        ) as resp:
            if resp.status == 201:
                data = await resp.json()
                keys.append(data["api_key"])
            else:
                text = await resp.text()
                print(f"  ERROR: Failed to register broker {i + 1}: {resp.status} {text}")
                return []
    return keys


async def _reset_db(
    session: aiohttp.ClientSession,
    admin_key: str = DEFAULT_ADMIN_KEY,
):
    async with session.post(
        "/debug/reset", headers=admin_headers(admin_key)
    ) as resp:
        if resp.status != 200:
            print(f"  WARNING: reset failed ({resp.status})")


async def _get_trade_count(
    session: aiohttp.ClientSession,
    admin_key: str = DEFAULT_ADMIN_KEY,
) -> int:
    """Query GET /debug/trade-count and return the count."""
    try:
        async with session.get(
            "/debug/trade-count", headers=admin_headers(admin_key)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["count"]
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Pre-market book seeding (via API)
# ---------------------------------------------------------------------------

async def seed_pre_market(
    session: aiohttp.ClientSession,
    keys: list[str],
    symbols: list[str],
    base_prices: dict[str, int],
    rng: random.Random,
) -> list[tuple[str, str]]:
    print(f"  Seeding pre-market book ({len(symbols) * 40:,} orders)...")
    start = time.perf_counter()
    sem = asyncio.Semaphore(50)
    order_ids: list[tuple[str, str]] = []
    lock = asyncio.Lock()
    count = 0

    async def place(body, key):
        nonlocal count
        async with sem:
            async with session.post(
                "/orders", headers=broker_header(key), json=body
            ) as resp:
                count += 1
                if resp.status == 201:
                    data = await resp.json()
                    async with lock:
                        order_ids.append((data["order_id"], key))
                else:
                    await resp.read()

    tasks = []
    for symbol in symbols:
        bp = base_prices[symbol]
        for i in range(20):
            ask_price = bp + max(bp * (1 + i * 9 // 19) // 100, 1)
            key = rng.choice(keys)
            tasks.append(place(limit_order("ask", ask_price, 500, symbol, rng), key))
            bid_price = bp - max(bp * (1 + i * 9 // 19) // 100, 1)
            key = rng.choice(keys)
            tasks.append(place(limit_order("bid", max(bid_price, 1), 500, symbol, rng), key))

    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start
    print(f"  Seeding complete: {count:,} orders seeded ({elapsed:.1f}s)")
    return order_ids


# ---------------------------------------------------------------------------
# Schedule generation
# ---------------------------------------------------------------------------

def _pick_valid_until_delta(rng: random.Random) -> float:
    """Return how many seconds from *send time* the order should remain valid."""
    r = rng.random()
    if r < DURATION_VERY_SHORT_PCT:
        return rng.uniform(0.5, 1.0)
    elif r < DURATION_VERY_SHORT_PCT + DURATION_SHORT_PCT:
        return rng.uniform(5.0, 10.0)
    else:
        return 86400.0  # 1 day


def build_schedule(
    duration: float,
    order_rate: float,
    cancel_rate: float,
    symbols: list[str],
    weights: list[float],
    base_prices: dict[str, int],
    keys: list[str],
    order_ids_for_reads: list[tuple[str, str]],
    rng: random.Random,
    no_reads: bool = False,
) -> list[dict]:
    # --- Pass 1: New orders (Poisson at order_rate) ---
    orders = []
    temp_id = 0
    t = 0.0
    while True:
        t += rng.expovariate(order_rate)
        if t >= duration:
            break

        key = rng.choice(keys)
        symbol = rng.choices(symbols, weights=weights, k=1)[0]
        bp = base_prices[symbol]

        r = rng.random()
        if r < PASSIVE_LIMIT_PCT:
            side = rng.choice(["bid", "ask"])
            if side == "bid":
                price = int(bp * rng.uniform(0.80, 0.95))
            else:
                price = int(bp * rng.uniform(1.05, 1.20))
            vu_delta = _pick_valid_until_delta(rng)
            body = limit_order(side, max(price, 1), rng.randint(50, 500), symbol, rng)
            orders.append({"time": t, "type": "write_no_match", "method": "POST", "path": "/orders", "key": key, "body": body, "symbol": symbol, "temp_id": temp_id, "is_limit": True, "vu_delta": vu_delta})
        elif r < PASSIVE_LIMIT_PCT + AGGRESSIVE_LIMIT_PCT:
            side = rng.choice(["bid", "ask"])
            if side == "bid":
                price = int(bp * rng.uniform(0.95, 1.05))
            else:
                price = int(bp * rng.uniform(0.95, 1.05))
            vu_delta = _pick_valid_until_delta(rng)
            body = limit_order(side, max(price, 1), rng.randint(50, 200), symbol, rng)
            orders.append({"time": t, "type": "write_match", "method": "POST", "path": "/orders", "key": key, "body": body, "symbol": symbol, "temp_id": temp_id, "is_limit": True, "vu_delta": vu_delta})
        else:
            side = rng.choice(["bid", "ask"])
            body = market_order(side, rng.randint(50, 200), symbol, rng)
            orders.append({"time": t, "type": "write_market", "method": "POST", "path": "/orders", "key": key, "body": body, "symbol": symbol, "temp_id": temp_id, "is_limit": False})
        temp_id += 1

    # --- Pass 2: Cancels ---
    limit_orders = [o for o in orders if o["is_limit"]]
    target_cancel_count = int(cancel_rate * duration)
    cancel_count = min(target_cancel_count, len(limit_orders))
    cancel_targets = rng.sample(limit_orders, cancel_count) if cancel_count > 0 else []

    cancels = []
    for target in cancel_targets:
        r = rng.random()
        if r < CANCEL_FAST_PCT:
            delay = rng.uniform(0.5, 5.0)
        elif r < CANCEL_FAST_PCT + CANCEL_MEDIUM_PCT:
            delay = rng.uniform(5.0, 60.0)
        else:
            delay = rng.uniform(60.0, 600.0)
        cancel_time = target["time"] + delay
        if cancel_time >= duration:
            continue
        cancels.append({
            "time": cancel_time,
            "type": "cancel",
            "method": "POST",
            "path": None,  # resolved at dispatch time
            "key": target["key"],
            "body": None,
            "symbol": target["symbol"],
            "cancel_target_id": target["temp_id"],
        })

    # --- Pass 3: Reads ---
    reads = []
    if not no_reads:
        read_rate = (order_rate + cancel_rate) * READ_MIX / (1.0 - READ_MIX)
        t = 0.0
        while True:
            t += rng.expovariate(read_rate)
            if t >= duration:
                break

            key = rng.choice(keys)
            symbol = rng.choices(symbols, weights=weights, k=1)[0]

            r = rng.random()
            if r < READ_ORDER_STATUS_PCT and order_ids_for_reads:
                oid, okey = rng.choice(order_ids_for_reads)
                reads.append({"time": t, "type": "read", "method": "GET", "path": f"/orders/{oid}", "key": okey, "body": None, "symbol": None})
            elif r < READ_ORDER_STATUS_PCT + READ_PRICE_PCT:
                reads.append({"time": t, "type": "read", "method": "GET", "path": f"/stocks/{symbol}/price", "key": key, "body": None, "symbol": symbol})
            elif r < READ_ORDER_STATUS_PCT + READ_PRICE_PCT + READ_BOOK_PCT:
                reads.append({"time": t, "type": "read", "method": "GET", "path": f"/stocks/{symbol}/book", "key": key, "body": None, "symbol": symbol})
            else:
                reads.append({"time": t, "type": "read", "method": "GET", "path": "/balance", "key": key, "body": None, "symbol": None})

    # --- Merge, sort, index ---
    schedule = orders + cancels + reads
    schedule.sort(key=lambda e: e["time"])

    # Build temp_id -> schedule_idx mapping for orders
    temp_id_to_idx: dict[int, int] = {}
    for idx, entry in enumerate(schedule):
        if "temp_id" in entry:
            entry["schedule_idx"] = idx
            temp_id_to_idx[entry["temp_id"]] = idx

    # Remap cancel targets from temp_id to schedule_idx
    for entry in schedule:
        if entry["type"] == "cancel":
            entry["cancel_target_idx"] = temp_id_to_idx[entry["cancel_target_id"]]
            del entry["cancel_target_id"]

    # Clean up temp fields from orders
    for entry in schedule:
        entry.pop("temp_id", None)
        entry.pop("is_limit", None)

    return schedule


# ---------------------------------------------------------------------------
# Schedule splitting for multiprocessing
# ---------------------------------------------------------------------------

def split_schedule(schedule: list[dict], num_workers: int) -> list[list[dict]]:
    """Symbol-based bin-packing: all events for a symbol go to the same worker.

    This guarantees cancel events land in the same worker as their target order,
    so order_futures resolution is always local.
    """
    if num_workers == 1:
        return [schedule]

    # Group events by symbol
    from collections import defaultdict
    by_symbol: dict[str | None, list[dict]] = defaultdict(list)
    for entry in schedule:
        by_symbol[entry.get("symbol")].append(entry)

    # Separate None-symbol events (balance reads) for round-robin
    no_symbol_events = by_symbol.pop(None, [])

    # Sort symbols by event count descending (greedy bin-packing)
    sorted_symbols = sorted(by_symbol.keys(), key=lambda s: len(by_symbol[s]), reverse=True)

    # Assign each symbol to the lightest worker
    chunks: list[list[dict]] = [[] for _ in range(num_workers)]
    worker_loads = [0] * num_workers
    for sym in sorted_symbols:
        lightest = min(range(num_workers), key=lambda w: worker_loads[w])
        chunks[lightest].extend(by_symbol[sym])
        worker_loads[lightest] += len(by_symbol[sym])

    # Round-robin no-symbol events to lightest workers
    for entry in no_symbol_events:
        lightest = min(range(num_workers), key=lambda w: worker_loads[w])
        chunks[lightest].append(entry)
        worker_loads[lightest] += 1

    # Sort each chunk by time and remap indices
    for chunk in chunks:
        chunk.sort(key=lambda e: e["time"])

        # Build old schedule_idx -> new chunk-local idx
        old_to_new: dict[int, int] = {}
        for new_idx, entry in enumerate(chunk):
            if "schedule_idx" in entry:
                old_to_new[entry["schedule_idx"]] = new_idx
                entry["schedule_idx"] = new_idx

        # Remap cancel_target_idx
        for entry in chunk:
            if "cancel_target_idx" in entry:
                entry["cancel_target_idx"] = old_to_new[entry["cancel_target_idx"]]

    return chunks


# ---------------------------------------------------------------------------
# Open-loop dispatch
# ---------------------------------------------------------------------------

async def run_open_loop(
    session: aiohttp.ClientSession,
    schedule: list[dict],
    duration: float,
    show_progress: bool = True,
) -> list[dict]:
    """Fire requests on schedule, record per-request timing. Returns raw records."""
    records: list[dict] = []
    sem = asyncio.Semaphore(500)
    pending: list[asyncio.Task] = []

    # Pre-create Futures for order events so cancels can await them
    order_futures: dict[int, asyncio.Future] = {}
    loop = asyncio.get_event_loop()
    for entry in schedule:
        if "schedule_idx" in entry and entry["type"].startswith("write"):
            order_futures[entry["schedule_idx"]] = loop.create_future()

    async def send_order(entry: dict, wall_start: float):
        async with sem:
            # Compute valid_until at send time so it's always in the future
            body = entry["body"]
            vu_delta = entry.get("vu_delta")
            if vu_delta is not None:
                body = dict(body)
                body["valid_until"] = (datetime.now(timezone.utc) + timedelta(seconds=vu_delta)).isoformat()
            send_time = time.perf_counter()
            try:
                async with session.request(
                    entry["method"], entry["path"],
                    headers=broker_header(entry["key"]),
                    json=body,
                ) as resp:
                    response_time = time.perf_counter()
                    order_id = None
                    if resp.status == 201 and "schedule_idx" in entry:
                        try:
                            data = await resp.json()
                            order_id = data.get("order_id")
                        except Exception:
                            await resp.read()
                    else:
                        await resp.read()
                    records.append({
                        "scheduled_time": entry["time"],
                        "send_time": send_time - wall_start,
                        "response_time": response_time - wall_start,
                        "type": entry["type"],
                        "status": resp.status,
                        "error": None,
                    })
                    # Resolve future for cancel awaiting
                    if "schedule_idx" in entry and entry["schedule_idx"] in order_futures:
                        fut = order_futures[entry["schedule_idx"]]
                        if not fut.done():
                            fut.set_result(order_id)
            except Exception as e:
                response_time = time.perf_counter()
                records.append({
                    "scheduled_time": entry["time"],
                    "send_time": send_time - wall_start,
                    "response_time": response_time - wall_start,
                    "type": entry["type"],
                    "status": None,
                    "error": type(e).__name__,
                })
                if "schedule_idx" in entry and entry["schedule_idx"] in order_futures:
                    fut = order_futures[entry["schedule_idx"]]
                    if not fut.done():
                        fut.set_result(None)

    async def send_read(entry: dict, wall_start: float):
        async with sem:
            send_time = time.perf_counter()
            try:
                async with session.request(
                    entry["method"], entry["path"],
                    headers=broker_header(entry["key"]),
                    json=entry["body"],
                ) as resp:
                    await resp.read()
                    response_time = time.perf_counter()
                    records.append({
                        "scheduled_time": entry["time"],
                        "send_time": send_time - wall_start,
                        "response_time": response_time - wall_start,
                        "type": entry["type"],
                        "status": resp.status,
                        "error": None,
                    })
            except Exception as e:
                response_time = time.perf_counter()
                records.append({
                    "scheduled_time": entry["time"],
                    "send_time": send_time - wall_start,
                    "response_time": response_time - wall_start,
                    "type": entry["type"],
                    "status": None,
                    "error": type(e).__name__,
                })

    async def send_cancel(entry: dict, wall_start: float):
        target_idx = entry["cancel_target_idx"]
        # Wait for the target order to complete and get its order_id
        fut = order_futures.get(target_idx)
        if fut is None:
            return  # target not in this worker's schedule
        try:
            order_id = await asyncio.wait_for(fut, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return  # skip if order didn't complete in time

        if order_id is None:
            return  # order failed, nothing to cancel

        async with sem:
            send_time = time.perf_counter()
            try:
                async with session.post(
                    f"/orders/{order_id}/cancel",
                    headers=broker_header(entry["key"]),
                ) as resp:
                    await resp.read()
                    response_time = time.perf_counter()
                    records.append({
                        "scheduled_time": entry["time"],
                        "send_time": send_time - wall_start,
                        "response_time": response_time - wall_start,
                        "type": "cancel",
                        "status": resp.status,
                        "error": None,
                    })
            except Exception as e:
                response_time = time.perf_counter()
                records.append({
                    "scheduled_time": entry["time"],
                    "send_time": send_time - wall_start,
                    "response_time": response_time - wall_start,
                    "type": "cancel",
                    "status": None,
                    "error": type(e).__name__,
                })

    wall_start = time.perf_counter()
    dispatched = 0
    last_progress = wall_start
    total = len(schedule)
    bar_width = 20

    for entry in schedule:
        now = time.perf_counter() - wall_start
        if now > duration:
            break

        delay = entry["time"] - now
        if delay > 0:
            await asyncio.sleep(delay)

        if entry["type"] == "cancel":
            task = asyncio.create_task(send_cancel(entry, wall_start))
        elif entry["type"] == "read":
            task = asyncio.create_task(send_read(entry, wall_start))
        else:
            task = asyncio.create_task(send_order(entry, wall_start))
        pending.append(task)
        dispatched += 1

        # Progress bar (update every ~1 second)
        if show_progress:
            wall_now = time.perf_counter()
            if wall_now - last_progress >= 1.0:
                last_progress = wall_now
                elapsed = wall_now - wall_start
                frac = min(elapsed / duration, 1.0)
                filled = int(bar_width * frac)
                bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
                sys.stderr.write(f"\r  [{bar}]  {elapsed:.0f}/{duration:.0f}s")
                sys.stderr.flush()

        if len(pending) > 1000:
            pending = [t for t in pending if not t.done()]

    # Clear progress bar
    if show_progress:
        sys.stderr.write("\r" + " " * 80 + "\r")
        sys.stderr.flush()

    # Drain
    pending = [t for t in pending if not t.done()]
    drain_start = time.perf_counter()
    if pending:
        done, still_pending = await asyncio.wait(pending, timeout=duration)
        if still_pending:
            for t in still_pending:
                t.cancel()
            await asyncio.wait(still_pending, timeout=2.0)

    drain_time = time.perf_counter() - drain_start
    wall_time = time.perf_counter() - wall_start

    return records, dispatched


# ---------------------------------------------------------------------------
# Multiprocessing worker
# ---------------------------------------------------------------------------

def _worker_entry(
    worker_id: int,
    url: str,
    schedule_chunk: list[dict],
    duration: float,
    result_queue: multiprocessing.Queue,
):
    """Entry point for each worker process. Runs its own event loop + aiohttp session."""

    async def _run():
        session = make_session(url)
        async with session:
            records, _dispatched = await run_open_loop(session, schedule_chunk, duration, show_progress=(worker_id == 0))
            result_queue.put((worker_id, records))

    if uvloop is not None:
        uvloop.run(_run())
    else:
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_results(records: list[dict], duration: float) -> dict:
    categories = {
        "write_no_match": [],
        "write_match": [],
        "write_market": [],
        "cancel": [],
        "read": [],
        "overall": [],
    }
    errors_by_type: dict[str, int] = {}
    total_errors = 0

    for r in records:
        if r["error"]:
            errors_by_type[r["error"]] = errors_by_type.get(r["error"], 0) + 1
            total_errors += 1
            continue
        if r["status"] not in (200, 201, 204, 404):
            key = f"HTTP_{r['status']}"
            errors_by_type[key] = errors_by_type.get(key, 0) + 1
            total_errors += 1
            continue

        uncorrected_ms = (r["response_time"] - r["send_time"]) * 1000

        rtype = r["type"]
        if rtype in categories:
            categories[rtype].append(uncorrected_ms)

        categories["overall"].append(uncorrected_ms)

    summary = {}
    for cat, lats in categories.items():
        summary[cat] = BenchmarkResult.percentiles(lats)

    # Time-series: 1-second buckets
    time_series = []
    num_buckets = int(math.ceil(duration)) + 5
    for bucket in range(num_buckets):
        bucket_lats = []
        bucket_writes = 0
        bucket_reads = 0
        bucket_errors = 0

        for r in records:
            t = r["scheduled_time"]
            if bucket <= t < bucket + 1:
                if r["error"] or (r["status"] and r["status"] not in (200, 201, 204, 404)):
                    bucket_errors += 1
                else:
                    lat_ms = (r["response_time"] - r["send_time"]) * 1000
                    bucket_lats.append(lat_ms)
                    if r["type"].startswith("write"):
                        bucket_writes += 1
                    else:
                        bucket_reads += 1

        if bucket_lats or bucket_errors:
            entry = {
                "bucket": f"{bucket}-{bucket + 1}s",
                "writes": bucket_writes,
                "reads": bucket_reads,
                "errors": bucket_errors,
            }
            entry.update(BenchmarkResult.percentiles(bucket_lats))
            time_series.append(entry)

    return {
        "summary": summary,
        "time_series": time_series,
        "errors": {"total": total_errors, "by_type": errors_by_type},
        "total_requests": len(records),
    }


# ---------------------------------------------------------------------------
# Post-run validation
# ---------------------------------------------------------------------------

async def post_run_validation(
    session: aiohttp.ClientSession,
    keys: list[str],
) -> dict:
    validation = {"passed": True, "checks": []}

    total_balance = 0
    for key in keys:
        try:
            async with session.get(
                "/balance", headers=broker_header(key)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    total_balance += data["balance"]
        except Exception:
            pass

    balance_ok = total_balance == 0
    validation["checks"].append({
        "name": "balance_invariant",
        "passed": balance_ok,
        "detail": f"SUM(balance) = {total_balance}",
    })
    if not balance_ok:
        validation["passed"] = False

    return validation


# ---------------------------------------------------------------------------
# ASCII time-series graphs
# ---------------------------------------------------------------------------

def _print_ascii_graph(title: str, time_series: list[dict], key: str, duration: float):
    """Print a compact horizontal bar chart from 1s time-series buckets aggregated to 5s."""
    bucket_size = 5
    num_buckets = int(math.ceil(duration / bucket_size))

    aggregated: list[tuple[str, float]] = []
    for i in range(num_buckets):
        start_s = i * bucket_size
        end_s = start_s + bucket_size
        label = f"{start_s}-{end_s}s"

        # Collect all latencies from 1s buckets in this range
        all_lats = []
        for ts in time_series:
            # Parse bucket start from "N-Ms" format
            bucket_start = int(ts["bucket"].split("-")[0])
            if start_s <= bucket_start < end_s:
                # Reconstruct latencies from percentiles (use the key value)
                if key in ts:
                    all_lats.append(ts[key])

        if all_lats:
            val = sum(all_lats) / len(all_lats)
            aggregated.append((label, val))
        else:
            aggregated.append((label, 0.0))

    # Filter out trailing zero buckets
    while aggregated and aggregated[-1][1] == 0.0:
        aggregated.pop()

    if not aggregated:
        return

    max_val = max(v for _, v in aggregated) if aggregated else 1.0
    bar_max_width = 30

    print(f"\n{title}:")
    for label, val in aggregated:
        if max_val > 0:
            bar_len = int(val / max_val * bar_max_width)
        else:
            bar_len = 0
        bar = "\u2588" * bar_len
        print(f"  {label:>8}  {bar:<{bar_max_width}}  {val:.1f}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results(analysis: dict, params: dict, validation: dict, wall_time: float, drain_time: float, trade_count: int):
    print(f"\n{'=' * 60}")
    print(f"{BOLD}RESULTS{RESET}")
    print(f"{'=' * 60}")

    summary = analysis["summary"]

    print(f"\n{'':>20} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'count':>8}")
    print("-" * 65)

    for cat in ["write_no_match", "write_match", "write_market", "cancel", "read", "overall"]:
        stats = summary.get(cat, {})
        if stats:
            display = CATEGORY_DISPLAY_NAMES.get(cat, cat)
            print(f"{display:>20} {stats['p50_ms']:>7.1f}ms {stats['p95_ms']:>7.1f}ms {stats['p99_ms']:>7.1f}ms {stats['max_ms']:>7.1f}ms {stats['count']:>8,}")

    # Trades
    total_orders = sum(
        summary.get(c, {}).get("count", 0)
        for c in ["write_no_match", "write_match", "write_market"]
    )
    trade_pct = (trade_count / total_orders * 100) if total_orders > 0 else 0.0
    print(f"\nTrades: {trade_count:,} ({trade_pct:.1f}% of orders)")

    # Errors with percentage
    errors = analysis["errors"]
    total_requests = analysis["total_requests"]
    if total_requests > 0:
        error_pct = errors["total"] / total_requests * 100
    else:
        error_pct = 0.0
    print(f"Errors: {errors['total']:,} ({error_pct:.2f}%)")
    if errors["by_type"]:
        for reason, cnt in sorted(errors["by_type"].items(), key=lambda x: -x[1]):
            print(f"  {reason}: {cnt:,}")

    # ASCII time-series graphs
    time_series = analysis["time_series"]
    if time_series:
        _print_ascii_graph("p50 latency (ms) over time", time_series, "p50_ms", wall_time)
        _print_ascii_graph("p99 latency (ms) over time", time_series, "p99_ms", wall_time)

    # Validation
    print(f"\nValidation:")
    for check in validation.get("checks", []):
        status = f"{GREEN}PASS{RESET}" if check["passed"] else f"{RED}FAIL{RESET}"
        print(f"  [{status}] {check['name']}: {check['detail']}")

    print(f"\nWall time: {wall_time:.1f}s  Drain time: {drain_time:.1f}s")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Realistic benchmark for the mini stock exchange")
    parser.add_argument("--version", required=True, help="Version label (e.g., v1, v2)")
    parser.add_argument("--url", default=DEFAULT_URL, help="API base URL")
    parser.add_argument("--admin-key", default=DEFAULT_ADMIN_KEY, help="Admin API key")
    parser.add_argument("--scale", type=int, default=25, help="Percent of B3 volume")
    parser.add_argument("--duration", type=float, default=60, help="Test duration in seconds")
    parser.add_argument("--no-reads", action="store_true", help="Disable read queries")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--workers", type=int, default=0, help="Worker processes (0=auto)")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    params = scale_params(args.scale)

    # Compute read rate for worker auto-calculation
    read_rate = 0.0 if args.no_reads else (params["order_rate"] + params["cancel_rate"]) * READ_MIX / (1.0 - READ_MIX)
    total_event_rate = params["order_rate"] + params["cancel_rate"] + read_rate

    # Auto-calculate workers
    if args.workers == 0:
        args.workers = min(max(1, int(total_event_rate) // 1500), os.cpu_count() or 4)

    print(f"\n{BOLD}Mini Stock Exchange — Realistic Benchmark{RESET}")
    print(f"  Version:   {args.version}")
    print(f"  Scale:     {args.scale}% of B3")
    print(f"  Duration:  {args.duration}s")
    print(f"  Symbols:   {params['symbols']}")
    print(f"  Brokers:   {params['brokers']}")
    print(f"  Orders:    {params['order_rate']:,}/sec")
    print(f"  Cancels:   {params['cancel_rate']:,}/sec")
    print(f"  Reads:     {'disabled' if args.no_reads else f'enabled ({READ_MIX:.0%})'}")
    print(f"  Workers:   {args.workers}")

    symbols = generate_symbols(params["symbols"])
    weights = zipf_weights(len(symbols), s=ZIPF_EXPONENT)
    base_prices = generate_base_prices(symbols, rng)

    # Setup phase (single session in main process)
    print(f"\n{BOLD}Setup{RESET}")
    async with make_session(args.url) as session:
        await _reset_db(session, args.admin_key)

        print(f"  Registering {params['brokers']} brokers...")
        keys = await _register_brokers(session, params["brokers"], args.admin_key)
        if not keys:
            print("ERROR: Failed to register brokers")
            return

        order_ids_for_reads = await seed_pre_market(session, keys, symbols, base_prices, rng)

        # Get trade count before run
        trade_count_before = await _get_trade_count(session, args.admin_key)

    # Generate schedule
    print(f"\n  Generating schedule...")
    schedule = build_schedule(
        args.duration, params["order_rate"], params["cancel_rate"],
        symbols, weights, base_prices,
        keys, order_ids_for_reads, rng, args.no_reads,
    )

    write_count = sum(1 for s in schedule if s["type"].startswith("write"))
    cancel_count = sum(1 for s in schedule if s["type"] == "cancel")
    read_count = sum(1 for s in schedule if s["type"] == "read")
    print(f"  Scheduled {len(schedule):,} events ({write_count:,} orders, {cancel_count:,} cancels, {read_count:,} reads)")

    # Run
    print(f"\n{BOLD}Running for {args.duration}s...{RESET}")

    if args.workers == 1:
        # Single-process: no multiprocessing overhead
        run_start = time.perf_counter()
        async with make_session(args.url) as session:
            records, _dispatched = await run_open_loop(session, schedule, args.duration)
        wall_time = time.perf_counter() - run_start
    else:
        # Multi-process
        chunks = split_schedule(schedule, args.workers)
        result_queue = multiprocessing.Queue()

        processes = []
        run_start = time.perf_counter()
        for i in range(args.workers):
            p = multiprocessing.Process(
                target=_worker_entry,
                args=(i, args.url, chunks[i], args.duration, result_queue),
            )
            p.start()
            processes.append(p)

        # Collect results from all workers
        records = []
        for _ in range(args.workers):
            _worker_id, worker_records = result_queue.get(
                timeout=args.duration + 60
            )
            records.extend(worker_records)

        for p in processes:
            p.join(timeout=10)

        wall_time = time.perf_counter() - run_start

    # Compute drain time from records
    if records:
        max_response = max(r["response_time"] for r in records)
        drain_time = max(0, max_response - args.duration)
    else:
        drain_time = 0

    # Analysis
    analysis = analyze_results(records, args.duration)

    # Get trade count after run
    print(f"\n{BOLD}Post-run validation{RESET}")
    async with make_session(args.url) as session:
        trade_count_after = await _get_trade_count(session, args.admin_key)
        trade_count = trade_count_after - trade_count_before

        validation = await post_run_validation(session, keys)

    # Print results
    print_results(analysis, params, validation, wall_time, drain_time, trade_count)

    # Save
    output = {
        "type": "realistic",
        "version": args.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "scale": args.scale,
            "duration": args.duration,
            "no_reads": args.no_reads,
            "seed": args.seed,
            "workers": args.workers,
            "client": "aiohttp",
            "uvloop": uvloop is not None,
            **params,
        },
        "summary": analysis["summary"],
        "time_series": analysis["time_series"],
        "trades": trade_count,
        "validation": validation,
        "wall_time": round(wall_time, 2),
        "drain_time": round(drain_time, 2),
        "errors": analysis["errors"],
    }
    save_results(output, f"{args.version}_realistic")


if __name__ == "__main__":
    if uvloop is not None:
        uvloop.run(main())
    else:
        asyncio.run(main())
