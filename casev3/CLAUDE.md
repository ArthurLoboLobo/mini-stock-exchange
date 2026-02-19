# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Mini Stock Exchange V3 — a Python/FastAPI order matching engine. V3 matches orders **in-memory** and serves **all reads from memory** (with DB fallback for pre-restart history). PostgreSQL is used only for durable persistence via async batches every ~30ms. This eliminates DB connection contention on read endpoints, targeting 4,000+ orders/sec.

Same API surface as V1/V2. Same matching rules. Different architecture.

## Commands

All commands run via docker-compose from this directory.

### Start services
```bash
docker-compose up --build
```
API at `http://localhost:8000`, Swagger docs at `/docs`. Alembic migrations run on startup. Single uvicorn worker (in-memory state cannot be shared across processes).

### Run tests
```bash
docker-compose exec api pytest -v
```
75 tests (39 unit + 36 API integration). Requires running PostgreSQL from docker-compose. `asyncio_mode = auto` in `pytest.ini`.

### Run a single test
```bash
docker-compose exec api pytest -v tests/test_api_matching.py::TestBasicMatching::test_same_price_match
```

### Database migrations
```bash
docker-compose exec api alembic upgrade head
docker-compose exec api alembic revision --autogenerate -m "description"
```

## Architecture

### Everything in memory + async persistence

```
POST /orders             → in-memory match (μs) → respond immediately → background DB batch (30ms)
POST /orders/{id}/cancel → in-memory cancel (μs) → 204 No Content → background DB batch (30ms)
GET  /orders/{id}        → memory-first (μs), DB fallback for pre-restart history
GET  /stocks/{symbol}/book  → pure in-memory (μs)
GET  /stocks/{symbol}/price → pure in-memory (μs)
GET  /balance            → pure in-memory (μs)
```

The `Engine` singleton (`app/engine/__init__.py`) holds all state:
- `orders: dict[UUID, Order]` — all orders (open AND closed, no eviction)
- `book: OrderBook` — `SortedDict` per symbol per side (asks sorted ascending, bids descending)
- `brokers_by_key_hash: dict[str, UUID]` — O(1) auth lookup (no DB query)
- `brokers: dict[UUID, BrokerInfo]` — broker name, balance, webhook_url (full broker state in memory)
- `trades_by_order: dict[UUID, list[Trade]]` — trades indexed by order ID for fast lookup
- `trade_prices: dict[str, deque[int]]` — recent trade prices per symbol (maxlen=1000)
- `queue: asyncio.Queue` — persistence items (frozen dataclass snapshots, not mutable references)

### Matching rules
- **Price-time priority**: best price first, FIFO within price level
- **Execution price is always the seller's price**
- **Partial fills**: order can match multiple counterparties in one call
- **Market orders**: IOC (immediate-or-cancel) — unfilled remainder is cancelled, never inserted into book
- **Lazy expiration**: expired counterparties removed during matching and on `GET /orders/{id}` reads

### Persistence pipeline (`app/engine/persistence.py`)

Queue items are frozen dataclass snapshots taken at enqueue time:
- `NewOrderItem` — pristine order snapshot before matching
- `TradeItem` — trade with broker context for balance updates and webhooks
- `OrderUpdateItem` — status/quantity change (deduplicated per order within a batch, last wins)

`flush_batch()` writes one transaction: INSERT orders → INSERT trades → UPDATE order statuses → UPDATE broker balances. After commit: fire webhooks (webhook URLs looked up from `engine.brokers` in memory). No eviction of closed orders.

### Write path (`POST /orders`)
After matching produces trades, the write path updates memory state immediately:
1. `engine.brokers[buyer/seller].balance` — adjusted for trade cost
2. `engine.trades_by_order[buy/sell_order_id]` — trade appended
3. `engine.trade_prices[symbol]` — price appended to deque

### Read paths
- **GET /orders/{id}** — memory-first from `engine.orders`, DB fallback via `async_session()` for pre-restart history
- **GET /stocks/{symbol}/book** — iterates `engine.book.asks/bids[symbol]` SortedDict directly
- **GET /stocks/{symbol}/price** — slices `engine.trade_prices[symbol]` deque
- **GET /balance** — reads `engine.brokers[broker_id].balance`

### Startup recovery (`app/main.py` lifespan)
1. Load full broker info into `engine.brokers_by_key_hash` AND `engine.brokers`
2. Load open non-expired orders into `engine.orders` + `engine.book` (FIFO order preserved)
3. Load trades for open orders into `engine.trades_by_order`
4. Load recent trade prices into `engine.trade_prices` (up to 1000 per symbol)
5. Start persistence loop background task

### Auth (`app/auth.py`)
- **Broker auth** (`get_current_broker_id`): SHA256 hash lookup in `engine.brokers_by_key_hash`. Returns `UUID`, not ORM model.
- **Admin auth** (`require_admin_key`): constant-time comparison against `EXCHANGE_ADMIN_API_KEY` env var. Used by all debug endpoints.

### Prices
Integers in cents (e.g., `3500` = $35.00).

### DB pool
Shrunk to `pool_size=2, max_overflow=2` since only the persistence loop and DB fallback reads need connections.

## Testing

### Key patterns

**Option A** (most tests): `await flush_persistence()` manually drains the queue and calls `flush_batch()`. Deterministic — no background task involved. In V3, flush is still used but GET reads work from memory regardless.

**Option B** (2 tests): `with_persistence_loop` fixture starts the real background task. Uses `await engine.queue.join()` to wait for completion.

**`ASGITransport` does NOT trigger ASGI lifespan.** The persistence loop and startup recovery do not run in tests. State is built manually through API calls + explicit flush.

### Fixtures (`tests/conftest.py`)
- `clean_state` — per-test: disposes stale DB pool, cancels orphaned persistence tasks, truncates tables, resets engine memory (including brokers, trades_by_order, trade_prices), sets admin key
- `client` — `httpx.AsyncClient` via `ASGITransport` (depends on `clean_state`)
- `broker_with_key` / `second_broker_with_key` — register broker via API, return `(broker_id, api_key)`. Registration populates `engine.brokers`.
- `with_persistence_loop` — starts background persistence task for Option B tests

### Helpers
- `make_limit_order(**overrides)` / `make_market_order(**overrides)` — valid order payloads with defaults
- `admin_header()` / `auth_header(api_key)` — Bearer token dicts

### Known subtleties
- Each test gets its own event loop (`asyncio_mode = auto`). `clean_state` disposes the app's SA engine pool to avoid stale connections. `_cancel_persistence_task()` checks `task.get_loop()` to handle orphaned tasks from dead event loops.
- `engine.clear()` drains the queue without calling `task_done()`, corrupting the unfinished-tasks counter. Fixtures replace the queue with a fresh `asyncio.Queue()` after `clear()`.

## Code Layout

- `app/main.py` — FastAPI app, lifespan (full broker loading, order recovery, trade loading, price loading, persistence loop start)
- `app/config.py` — Pydantic Settings with `EXCHANGE_` prefix
- `app/database.py` — SQLAlchemy async engine + session factory (pool_size=2, max_overflow=2)
- `app/models.py` — SQLAlchemy ORM: `Broker`, `Order`, `Trade` with enums
- `app/schemas.py` — Pydantic request/response schemas
- `app/auth.py` — API key authentication (`get_current_broker_id`, `require_admin_key`)
- `app/middleware.py` — `SlowRequestMiddleware` (pure ASGI, >100ms threshold)
- `app/engine/__init__.py` — `Engine` singleton (orders, book, brokers_by_key_hash, brokers, trades_by_order, trade_prices, queue, persistence_task) + `BrokerInfo` dataclass
- `app/engine/matching.py` — Matching logic (`match_order`, `_match_bid`, `_match_ask`)
- `app/engine/order_book.py` — `OrderBook` with SortedDict per symbol/side
- `app/engine/persistence.py` — Queue items (`NewOrderItem`, `TradeItem`, `OrderUpdateItem`), `flush_batch()`, `run_persistence_loop()`. Webhook URLs from `engine.brokers` (no DB query).
- `app/routers/orders.py` — POST/GET/cancel order endpoints. POST updates in-memory state (balances, trades, prices) after matching. GET is memory-first with DB fallback. Cancel is pure in-memory (204 no-op for non-existent/closed).
- `app/routers/brokers.py` — Broker registration (`POST /register` populates `engine.brokers`), balance (`GET /balance` from memory)
- `app/routers/stocks.py` — Stock price and order book served entirely from memory (no DB dependency)
- `app/routers/debug.py` — Trade count, state reset (admin-only, stops/restarts persistence loop)
- `app/services/webhooks.py` — Async webhook delivery (`send_webhook`, `fire_webhooks`)
- `tests/conftest.py` — Fixtures: `clean_state`, `client`, `broker_with_key`, `second_broker_with_key`, `with_persistence_loop`, helpers
- `tests/test_matching.py` — Unit tests for matching logic (20 tests)
- `tests/test_order_book.py` — Unit tests for OrderBook (13 tests)
- `tests/test_persistence.py` — Unit tests for persistence pipeline (6 tests)
- `tests/test_api_matching.py` — API integration tests for order matching, persistence, and cancel (25 tests)
- `tests/test_api_brokers.py` — API integration tests for broker registration (8 tests)
- `tests/test_api_debug.py` — API integration tests for debug endpoints (4 tests)

## Environment

- `EXCHANGE_DATABASE_URL` — PostgreSQL connection string (`postgresql+asyncpg://...`)
- `EXCHANGE_ADMIN_API_KEY` — Admin key for `/register` and `/debug/reset` (returns 503 if unset)
- `EXCHANGE_ECHO_SQL` — Enable SQLAlchemy SQL logging (default: `false`)
- Config loaded via Pydantic Settings with `EXCHANGE_` prefix (`app/config.py`)
