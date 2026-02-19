"""
Shared utilities for the benchmark suite.

Constants, helpers, WebhookSink, BenchmarkResult, ANSI colors,
broker registration, DB reset, timed requests, result saving.
"""
import asyncio
import json
import random
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ADMIN_KEY = "admin-secret-key-temporary"
DEFAULT_URL = "http://localhost:8000"

REAL_TICKERS = [
    "PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3", "WEGE3", "RENT3", "BBAS3",
]

DOCUMENTS = [f"{i:011d}" for i in range(100)]

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def pass_str() -> str:
    return f"{GREEN}PASS{RESET}"


def fail_str() -> str:
    return f"{RED}FAIL{RESET}"


# ---------------------------------------------------------------------------
# Symbol / price generation
# ---------------------------------------------------------------------------

def generate_symbols(count: int) -> list[str]:
    symbols = REAL_TICKERS[:min(count, len(REAL_TICKERS))]
    for i in range(len(symbols), count):
        symbols.append(f"STK{i + 1:04d}")
    return symbols


def zipf_weights(n: int, s: float = 1.2) -> list[float]:
    raw = [1.0 / (rank ** s) for rank in range(1, n + 1)]
    total = sum(raw)
    return [w / total for w in raw]


def generate_base_prices(symbols: list[str], rng: random.Random) -> dict[str, int]:
    return {s: rng.randint(500, 20000) for s in symbols}


# ---------------------------------------------------------------------------
# Order payload builders
# ---------------------------------------------------------------------------

def limit_order(
    side: str,
    price: int,
    quantity: int = 100,
    symbol: str | None = None,
    rng: random.Random | None = None,
    valid_until: str | None = None,
) -> dict:
    r = rng or random.Random()
    return {
        "document_number": r.choice(DOCUMENTS),
        "side": side,
        "order_type": "limit",
        "symbol": symbol or "PETR4",
        "price": price,
        "quantity": quantity,
        "valid_until": valid_until or (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }


def market_order(
    side: str,
    quantity: int = 100,
    symbol: str | None = None,
    rng: random.Random | None = None,
) -> dict:
    r = rng or random.Random()
    return {
        "document_number": r.choice(DOCUMENTS),
        "side": side,
        "order_type": "market",
        "symbol": symbol or "PETR4",
        "quantity": quantity,
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def admin_headers(admin_key: str = DEFAULT_ADMIN_KEY) -> dict:
    return {"Authorization": f"Bearer {admin_key}"}


def broker_header(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def random_broker_header(keys: list[str], rng: random.Random) -> dict:
    return {"Authorization": f"Bearer {rng.choice(keys)}"}


# ---------------------------------------------------------------------------
# WebhookSink
# ---------------------------------------------------------------------------

class _WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.send_response(200)
        self.end_headers()
        server = self.server  # type: ignore[attr-defined]
        with server._lock:
            server._webhook_count += 1
            if server._store_payloads:
                try:
                    server._payloads.append(json.loads(body))
                except Exception:
                    pass

    def log_message(self, format, *args):
        pass


class WebhookSink:
    def __init__(self, store_payloads: bool = False):
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._store_payloads = store_payloads

    def start(self):
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), _WebhookHandler)
        self._server._lock = threading.Lock()  # type: ignore[attr-defined]
        self._server._webhook_count = 0  # type: ignore[attr-defined]
        self._server._store_payloads = self._store_payloads  # type: ignore[attr-defined]
        self._server._payloads = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://host.docker.internal:{self.port}/webhook"

    @property
    def count(self) -> int:
        if self._server is None:
            return 0
        with self._server._lock:  # type: ignore[attr-defined]
            return self._server._webhook_count  # type: ignore[attr-defined]

    @property
    def payloads(self) -> list[dict]:
        if self._server is None:
            return []
        with self._server._lock:  # type: ignore[attr-defined]
            return list(self._server._payloads)  # type: ignore[attr-defined]

    def reset(self):
        if self._server is not None:
            with self._server._lock:  # type: ignore[attr-defined]
                self._server._webhook_count = 0  # type: ignore[attr-defined]
                self._server._payloads = []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Poll helper (V2 eventual consistency)
# ---------------------------------------------------------------------------

async def poll(check_fn, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check_fn():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Timed request
# ---------------------------------------------------------------------------

async def timed_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict,
    json_body: dict | None = None,
) -> tuple[float, httpx.Response | None]:
    start = time.perf_counter()
    try:
        resp = await client.request(method, url, headers=headers, json=json_body)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return elapsed_ms, resp
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return elapsed_ms, None


# ---------------------------------------------------------------------------
# Broker registration & DB reset
# ---------------------------------------------------------------------------

async def register_brokers(
    client: httpx.AsyncClient,
    count: int,
    admin_key: str = DEFAULT_ADMIN_KEY,
    webhook_url: str | None = None,
) -> list[str]:
    keys = []
    for i in range(count):
        body = {"name": f"Bench Broker {i + 1}"}
        if webhook_url:
            body["webhook_url"] = webhook_url
        resp = await client.post("/register", json=body, headers=admin_headers(admin_key))
        if resp.status_code == 201:
            keys.append(resp.json()["api_key"])
        else:
            print(f"  ERROR: Failed to register broker {i + 1}: {resp.status_code}")
            return []
    return keys


async def reset_db(client: httpx.AsyncClient, admin_key: str = DEFAULT_ADMIN_KEY):
    resp = await client.post("/debug/reset", headers=admin_headers(admin_key))
    if resp.status_code != 200:
        print(f"  WARNING: reset failed ({resp.status_code})")


# ---------------------------------------------------------------------------
# BenchmarkResult
# ---------------------------------------------------------------------------

class BenchmarkResult:
    def __init__(self, name: str):
        self.name = name
        self.latencies: list[float] = []
        self.corrected_latencies: list[float] = []
        self.errors: int = 0

    def record(self, latency_ms: float):
        self.latencies.append(latency_ms)

    def record_corrected(self, latency_ms: float):
        self.corrected_latencies.append(latency_ms)

    def record_error(self):
        self.errors += 1

    @staticmethod
    def percentiles(latencies: list[float]) -> dict:
        if not latencies:
            return {}
        s = sorted(latencies)
        n = len(s)
        return {
            "count": n,
            "min_ms": round(s[0], 2),
            "p50_ms": round(s[int(n * 0.50)], 2),
            "p95_ms": round(s[int(n * 0.95)], 2),
            "p99_ms": round(s[min(int(n * 0.99), n - 1)], 2),
            "max_ms": round(s[-1], 2),
            "avg_ms": round(statistics.mean(s), 2),
        }

    def summary(self) -> dict:
        result = {"name": self.name, "errors": self.errors}
        result.update(self.percentiles(self.latencies))
        if self.corrected_latencies:
            result["corrected"] = self.percentiles(self.corrected_latencies)
        return result


# ---------------------------------------------------------------------------
# Result saving
# ---------------------------------------------------------------------------

def save_results(data: dict, prefix: str):
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    filepath = results_dir / f"{prefix}_{timestamp}.json"

    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

    latest_path = results_dir / f"{prefix}_latest.json"
    with open(latest_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nResults saved to {filepath}")
    print(f"Latest: {latest_path}")
