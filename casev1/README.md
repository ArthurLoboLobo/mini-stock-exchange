# Mini Stock Exchange — V1

A mini stock exchange system that receives and processes orders from brokers, matches them using price-time priority, and executes trades.

## Features

- **Order submission** — Brokers submit buy (bid) and sell (ask) orders via REST API
- **Order cancellation** — Cancel open limit orders (market orders cannot be cancelled)
- **Matching engine** — Automatic order matching with price-time priority (best price first, FIFO to break ties)
- **Partial fills** — Orders can be partially executed across multiple counterparties
- **Market orders** — Immediate-or-cancel orders with no price limit
- **Order book** — Aggregated view of open orders per price level
- **Stock price** — Last trade price and moving average
- **Broker balance** — Net cash position from all executed trades
- **Webhooks** — Real-time trade execution notifications to brokers
- **Broker registration** — Admin-protected endpoint to register new brokers via API
- **Health check** — `GET /health` for Docker and load balancer probes

## Tech Stack

- **Python 3.12 + FastAPI** (async)
- **PostgreSQL 16** (source of truth + matching engine)
- **SQLAlchemy + asyncpg** (async ORM)
- **Alembic** (migrations)
- **Docker + docker-compose**

## Quick Start

```bash
docker-compose up --build
```

The API will be available at `http://localhost:8000`. Migrations run automatically on startup.

API docs (Swagger UI) are at `http://localhost:8000/docs`.

**Admin API Key:** For demo purposes, the admin key is set to `admin-secret-key-temporary` in docker-compose.yml. This is used for broker registration via `POST /register`. In production, this should be a secure secret.

### Run tests

Tests require a running PostgreSQL instance (provided by docker-compose):

```bash
docker-compose exec api pytest -v
```

## API

All endpoints (except `/health`) require authentication via `Authorization: Bearer <api_key>`.

### Admin

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/register` | Register a new broker (admin key required) |

### Core

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check (no auth) |
| POST | `/orders` | Submit a new order |
| GET | `/orders/{id}` | Get order status and trade history |

### Extensions

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/orders/{id}/cancel` | Cancel an open limit order |
| GET | `/stocks/{symbol}/price?trades=50` | Current stock price (moving average) |
| GET | `/stocks/{symbol}/book?depth=10` | Order book (aggregated by price level) |
| GET | `/balance` | Broker's net cash balance |

### Debug

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/debug/trade-count` | admin only | Return the current number of trades in the database |
| POST | `/debug/reset` | admin only | Delete all data — full database reset for benchmarks |

### Register a broker

Requires the admin API key (default: `admin-secret-key-temporary` — see docker-compose.yml).

> Note: `<api_url>` is `http://localhost:8000` if running locally via Docker.

```bash
curl -X POST <api_url>/register \
  -H "Authorization: Bearer admin-secret-key-temporary" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Broker", "webhook_url": "https://example.com/hook"}'
```

Response (`201 Created`):
```json
{"broker_id": "uuid-here", "api_key": "key-uuid-here"}
```

Save the API key — it cannot be retrieved again.

### Submit an order

```bash
curl -X POST <api_url>/orders \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "document_number": "12345678901",
    "side": "bid",
    "symbol": "PETR4",
    "price": 3500,
    "quantity": 1000,
    "valid_until": "2026-02-15T23:59:59Z"
  }'
```

Response: `{"order_id": "uuid-here"}`

#### Limit Order

The `order_type` field defaults to `"limit"` if omitted.

```json
{
  "document_number": "12345678901",
  "side": "bid",
  "order_type": "limit",
  "symbol": "PETR4",
  "price": 3500,
  "quantity": 1000,
  "valid_until": "2026-02-15T23:59:59Z"
}
```

#### Market Order

Market orders must **not** include a `price` field (or set it to `null`). They are Immediate-Or-Cancel (IOC).

```json
{
  "document_number": "12345678901",
  "side": "ask",
  "order_type": "market",
  "symbol": "PETR4",
  "quantity": 500
}
```

### Check order status

```bash
curl <api_url>/orders/<order_id> \
  -H "Authorization: Bearer <api_key>"
```

### Cancel an order

Only open limit orders can be cancelled. Cancelling a market order or an already-closed order is a **no-op** (returns 204 Success).

```bash
curl -X POST <api_url>/orders/<order_id>/cancel \
  -H "Authorization: Bearer <api_key>"
```

Response: `204 No Content` (empty body).

### Get Stock Price

Returns the last trade price and a moving average of recent trades. You can specify the number of recent trades to average via the `trades` query parameter (default: 50, max: 1000).

```bash
curl "<api_url>/stocks/PETR4/price?trades=10" \
  -H "Authorization: Bearer <api_key>"
```

Response:
```json
{
  "symbol": "PETR4",
  "last_price": 3550,
  "average_price": 3540,
  "trades_in_average": 10
}
```

### Get Order Book

Returns the current open orders aggregated by price level. You can specify the depth (price levels per side) via the `depth` query parameter (default: 10, max: 50).

```bash
curl "<api_url>/stocks/PETR4/book?depth=20" \
  -H "Authorization: Bearer <api_key>"
```

Response:
```json
{
  "symbol": "PETR4",
  "depth": 20,
  "asks": [
    {"price": 3600, "total_quantity": 500, "order_count": 2},
    {"price": 3610, "total_quantity": 1000, "order_count": 1}
  ],
  "bids": [
    {"price": 3590, "total_quantity": 200, "order_count": 1},
    {"price": 3580, "total_quantity": 1500, "order_count": 3}
  ]
}
```

### Get Broker Balance

Returns the net cash balance for the authenticated broker (Sum of Sells - Sum of Buys).

```bash
curl <api_url>/balance \
  -H "Authorization: Bearer <api_key>"
```

Response:
```json
{
  "broker_id": "uuid-here",
  "broker_name": "My Broker",
  "balance": 1500000
}
```

### Webhooks

If a broker registers a `webhook_url`, the system sends a `POST` request to that URL whenever a trade occurs involving their order (either as the incoming aggressor or the resting passive order).

**Payload:**

```json
{
  "event": "trade_executed",
  "trade_id": "uuid-here",
  "order_id": "uuid-here",
  "symbol": "PETR4",
  "side": "bid",
  "price": 3500,
  "quantity": 100,
  "order_remaining_quantity": 900,
  "executed_at": "2026-02-18T10:00:00Z"
}
```

## Data Models

### Order Schema

Fields required for `POST /orders`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_number` | string | Yes | Customer document ID (CPF/CNPJ). Max 20 chars. |
| `side` | enum | Yes | `bid` (buy) or `ask` (sell). |
| `order_type` | enum | No | `limit` (default) or `market`. |
| `symbol` | string | Yes | Stock symbol (e.g., "PETR4"). Max 10 chars. |
| `price` | integer | Conditional | Price in cents. **Required** for Limit orders. **Forbidden** for Market orders. |
| `quantity` | integer | Yes | Number of shares. Must be > 0. |
| `valid_until` | datetime | Conditional | ISO8601 UTC. **Required** for Limit orders. Ignored for Market orders. |

## Error Handling

Common HTTP error responses:

| Status Code | Description |
|-------------|-------------|
| `400 Bad Request` | Invalid JSON or malformed request. |
| `401 Unauthorized` | Missing or invalid API key. |
| `403 Forbidden` | Accessing an order belonging to another broker. |
| `404 Not Found` | Order or stock symbol not found. |
| `422 Unprocessable Entity` | Validation error (e.g., price on market order, past valid_until). |


## Architecture

- **Database-driven matching** — No in-memory order books. Matching runs as indexed queries within a single DB transaction with row-level locking.
- **Atomic transactions** — Each incoming order's entire matching loop (insert + N matches + N trades) is a single DB transaction. Crash = rollback, no corrupt state.
- **Periodic cleanup** — Background task expires stale orders every 60 seconds to keep the partial index compact.

## Assumptions

- ~100 brokers, ~450 stocks
- ~4M trades/day (~140 trades/sec)
- ~700 new orders/sec
- Prices stored as integers (cents), 2 decimal places
- Matching priority: price-time (best price first, FIFO for ties)
- Execution price is always the seller's price
- Brokers are registered via `POST /register` (protected by admin API key)
- No stock ownership validation (brokers are trusted)
