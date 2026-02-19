# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Mini Stock Exchange V1 — a Python/FastAPI order matching engine backed by PostgreSQL. Brokers submit buy/sell orders via REST API; the engine matches them using price-time priority and executes trades atomically in the database.

## Commands

All commands run via docker-compose from this directory.

### Start services
```bash
docker-compose up --build
```
API at `http://localhost:8000`, Swagger docs at `/docs`. Alembic migrations run automatically on startup.

### Run tests
```bash
docker-compose exec api pytest -v
```
Tests require the running PostgreSQL from docker-compose. Test DB is cleaned per-test (DELETE FROM all tables). pytest-asyncio is configured with `asyncio_mode = auto`.

### Run a single test
```bash
docker-compose exec api pytest -v tests/test_matching.py::test_name
```

### Database migrations
```bash
docker-compose exec api alembic upgrade head     # apply
docker-compose exec api alembic revision --autogenerate -m "description"  # create new
```

## Architecture

### Database-driven matching (no in-memory order book)

The matching engine (`app/services/matching.py`) runs entirely as indexed SQL queries within a single DB transaction. `match_order()` loops: find best counterparty via `SELECT ... FOR UPDATE`, execute trade, repeat until no more matches or quantity exhausted. The entire loop (insert order + N matches + N trades) is one atomic transaction — crash = full rollback.

### Matching rules
- **Price-time priority**: best price first, FIFO to break ties
- **Execution price is always the seller's price** (per case spec)
- **Partial fills**: an order can match against multiple counterparties
- **Market orders**: IOC behavior (immediate-or-cancel, no `valid_until` or `price`)

### Key indexes
- Partial index on `(symbol, side, price, created_at) WHERE status = 'open'` — the critical matching query index
- Index on `(broker_id, created_at)` for broker order lookups
- Background task in `app/tasks.py` expires stale orders every 60s to keep the partial index compact

### Connection pool
`pool_size=20, max_overflow=30` configured in `app/database.py`. Uvicorn runs with 4 workers (set in docker-compose).

### Prices
Stored as integers in cents (e.g., `3500` = $35.00). Max 2 decimal places.

### Auth
Two auth dependencies in `app/auth.py`:
- **Broker auth** (`get_current_broker`): Bearer token looked up via SHA256 hash in the `brokers` table. Uses a TTL cache (60s). Used by most endpoints.
- **Admin auth** (`require_admin_key`): Bearer token compared (constant-time) against `EXCHANGE_ADMIN_API_KEY` env var. Used by `POST /register` and all debug endpoints. Returns 503 if the admin key is not configured.

### Webhooks
Fire-and-forget async POST to broker's `webhook_url` after trade commit (`app/services/webhooks.py`). Non-blocking — failures are silently logged.

### Order expiration
Dual mechanism: periodic cleanup task (every 60s in `app/tasks.py`) + lazy check on `GET /orders/{id}` (closes if expired when read).

## Code Layout

- `app/main.py` — FastAPI app, lifespan (background cleanup task)
- `app/config.py` — Pydantic Settings with `EXCHANGE_` prefix
- `app/database.py` — SQLAlchemy async engine + session factory (`pool_size=20, max_overflow=30`)
- `app/models.py` — SQLAlchemy models: `Broker`, `Order`, `Trade` with enums (`OrderSide`, `OrderType`, `OrderStatus`)
- `app/schemas.py` — Pydantic request/response schemas
- `app/auth.py` — API key authentication (`get_current_broker`, `require_admin_key`)
- `app/middleware.py` — `SlowRequestMiddleware` logs requests taking >100ms
- `app/tasks.py` — Background order expiration cleanup
- `app/services/matching.py` — Core matching engine (`match_order`, `_find_best_match`, `_execute_trade`)
- `app/services/webhooks.py` — Async webhook delivery (`send_webhook`, `fire_webhooks`)
- `app/routers/orders.py` — POST/GET/cancel order endpoints (`POST /orders`, `GET /orders/{id}`, `POST /orders/{id}/cancel` returns 204 No Content with no body — non-existent/already-closed orders are silent no-ops)
- `app/routers/brokers.py` — Broker registration (`POST /register`), balance (`GET /balance`)
- `app/routers/stocks.py` — Stock price (`GET /stocks/{symbol}/price`), order book (`GET /stocks/{symbol}/book`)
- `app/routers/debug.py` — Trade count, state reset (admin-only)
- `tests/conftest.py` — Fixtures: `db`, `client`, `broker_with_key`, `second_broker_with_key`, helpers `make_limit_order()` / `make_market_order()`, `auth_header()`, `TEST_ADMIN_KEY`
- `tests/test_matching.py` — Matching engine tests (basic matching, partial fills, all case spec scenarios)
- `tests/test_orders.py` — Order creation and validation tests
- `tests/test_extensions.py` — Stock price, order book, broker balance endpoint tests
- `tests/test_brokers.py` — Broker registration tests (admin auth, validation, end-to-end)
- `tests/test_debug.py` — Debug endpoint tests (database reset)

## Testing Conventions

- Tests use `httpx.AsyncClient` with `ASGITransport` — no real HTTP server needed
- DB dependency is overridden in fixtures via `app.dependency_overrides[get_db]`
- Test helpers `make_limit_order(**overrides)` and `make_market_order(**overrides)` build valid payloads with sensible defaults
- `auth_header(api_key)` returns the auth dict for requests

## Environment

- `EXCHANGE_DATABASE_URL` — PostgreSQL connection string (default via docker-compose: `postgresql+asyncpg://exchange:exchange@db:5432/exchange`)
- `EXCHANGE_ADMIN_API_KEY` — Global admin key for `POST /register` (must be set or the endpoint returns 503)
- `EXCHANGE_ECHO_SQL` — Boolean, enables SQLAlchemy SQL query logging (default: `false`)
- Config loaded via Pydantic Settings with `EXCHANGE_` prefix (`app/config.py`)
