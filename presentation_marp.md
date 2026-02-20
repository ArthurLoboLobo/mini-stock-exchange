---
marp: true
theme: default
paginate: true
style: |
  section {
    font-family: 'Inter', 'Segoe UI', sans-serif;
    font-size: 28px;
    background-color: #FAFBFC;
    color: #1a1a1a;
  }
  h1 {
    color: #053A22;
    font-size: 48px;
  }
  h2 {
    color: #053A22;
    font-size: 36px;
  }
  h3 {
    color: #053A22;
  }
  table {
    font-size: 22px;
    margin: 0 auto;
  }
  th {
    background-color: #053A22;
    color: #FAFBFC;
  }
  td, th {
    padding: 8px 16px;
  }
  td {
    border-color: #d0d7de;
  }
  code {
    font-size: 22px;
    color: #053A22;
    background-color: #e6f0eb;
  }
  pre {
    font-size: 20px;
    background-color: #f0f4f2;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 20px;
  }
  pre code {
    background-color: transparent;
    color: #1a1a1a;
  }
  blockquote {
    border-left: 4px solid #053A22;
    color: #444;
    font-style: italic;
  }
  strong {
    color: #053A22;
  }
  a {
    color: #053A22;
  }
---

# Mini Stock Exchange

### Engineering Case — Arthur Lobo

<!-- _class: lead invert -->

<!--
Keep it clean. Brief intro, state your name and the project.
-->

---

## The Challenge

- Build a mini stock exchange
- Accept orders, match buyers to sellers, execute trades
- **Write:** submit orders (limit + market), cancel orders
- **Read:** order status, order book, stock price, broker balance

<!--
"The task: build a stock exchange. Accept orders, match buyers to sellers, execute trades."
"I don't have much experience developing APIs, so this was a big learning curve for me."
I also built the extension features: order book queries, stock pricing, broker balances, webhooks, market orders.
-->

---

## API Endpoints

<style scoped>
table { font-size: 24px; }
td, th { padding: 9px 18px; }
</style>

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/register` | Register a new broker (admin only) |
| POST | `/orders` | Submit a limit or market order |
| GET | `/orders/{id}` | Order status + trade history |
| POST | `/orders/{id}/cancel` | Cancel an open limit order |
| GET | `/stocks/{symbol}/price` | Last trade price + moving average |
| GET | `/stocks/{symbol}/book` | Order book (aggregated by price level) |
| GET | `/balance` | Broker's net cash balance |

<!--
"These are all the production endpoints. Webhooks notify brokers on trade execution."
-->

---

## B3 Reference Numbers

<style scoped>
table { font-size: 24px; }
td, th { padding: 9px 18px; }
</style>

| B3 Metric | Value | Source |
|-----------|-------|--------|
| Daily trades | ~4 million (~140/sec) | B3 daily market bulletin |
| Daily orders | ~20 million (~700/sec) | Estimate — 5:1 order-to-trade ratio |
| Symbols | ~450 | B3 listed stocks |
| Brokers | ~100 | B3 registered brokers |
| Traffic mix | ~53% orders, ~19% cancel, ~28% read | Estimate from other exchanges |

<!--
"Before writing code, I looked up how much B3 actually handles in a day."
Trades: ~4M/day from B3's daily market bulletin.
Orders: 5:1 order-to-trade ratio (conservative for a low-HFT market) → 20M orders/day ÷ 8h trading day ≈ 700/sec.
-->

---

## How I Tested It

**Full Realistic Simulation**
- Sends requests at B3's per-second rates for a 60-second window
- Randomly spaced requests to simulate bursts.
- A few symbols get most of the orders, just like real markets.
- Controls **what % of B3 traffic** to send (e.g., 100% = full B3 load)
- Outputs a full report: latency percentiles, time series, and error rates

**Correctness Tests**: Verifies trading logic is correct, untimed.

**Micro-Benchmarks**: Times individual operations.

<!--
"I built three test tools, but the one that matters is the full simulation — it replays a compressed B3 trading day against the API."
-->

---

## V1: Everything in the Database

```
Client → FastAPI → PostgreSQL
                   (matching + storage)
```

- Stateless API + PostgreSQL
- Every order = one database transaction
- Row-level locking keeps things consistent
- Orders for the same symbol processed sequentially

<!--
"My first approach: every order goes through PostgreSQL. One transaction per order. The database does the matching and the storage."
-->

---

## V1: Database Schema

<style scoped>
.cols { display: flex; gap: 24px; margin-top: 16px; }
.cols > div { flex: 1; }
table { width: 100%; border-collapse: collapse; font-size: 18px; }
th, td { padding: 5px 10px; text-align: left; }
th { background-color: #053A22; color: #FAFBFC; }
td { border-color: #d0d7de; }
td:first-child { font-family: 'Courier New', monospace; }
.tbl-title { background-color: #053A22; color: #FAFBFC; font-size: 22px; font-weight: bold; text-align: center; padding: 7px; }
</style>

<div class="cols">
  <div>
    <table>
      <tr><td class="tbl-title" colspan="2">brokers</td></tr>
      <tr><th>Column</th><th>Type</th></tr>
      <tr><td>id</td><td>UUID PK</td></tr>
      <tr><td>name</td><td>String</td></tr>
      <tr><td>api_key_hash</td><td>String</td></tr>
      <tr><td>webhook_url</td><td>String</td></tr>
      <tr><td>created_at</td><td>Timestamp</td></tr>
    </table>
  </div>
  <div>
    <table>
      <tr><td class="tbl-title" colspan="2">orders</td></tr>
      <tr><th>Column</th><th>Type</th></tr>
      <tr><td>id</td><td>UUID PK</td></tr>
      <tr><td>broker_id</td><td>UUID FK</td></tr>
      <tr><td>side</td><td>bid / ask</td></tr>
      <tr><td>order_type</td><td>limit / market</td></tr>
      <tr><td>symbol</td><td>String</td></tr>
      <tr><td>price</td><td>Integer</td></tr>
      <tr><td>quantity</td><td>Integer</td></tr>
      <tr><td>remaining_quantity</td><td>Integer</td></tr>
      <tr><td>valid_until</td><td>Timestamp</td></tr>
      <tr><td>status</td><td>open / closed</td></tr>
    </table>
  </div>
  <div>
    <table>
      <tr><td class="tbl-title" colspan="2">trades</td></tr>
      <tr><th>Column</th><th>Type</th></tr>
      <tr><td>id</td><td>UUID PK</td></tr>
      <tr><td>buy_order_id</td><td>UUID FK</td></tr>
      <tr><td>sell_order_id</td><td>UUID FK</td></tr>
      <tr><td>symbol</td><td>String</td></tr>
      <tr><td>price</td><td>Integer</td></tr>
      <tr><td>quantity</td><td>Integer</td></tr>
    </table>
  </div>
</div>

---

## V1: Results

### ~25% of B3 (175 orders/second)

**Bottleneck: Database Locks**

- Multiple brokers trading the same stock → requests wait in line → latency spikes
- The DB was doing two jobs: **storage** AND **matching**
- Matching is the expensive one — more complex and frequent.

<!--
"It worked correctly, but under load it fell apart."
"The matching engine running inside the database was the bottleneck — too much locking overhead to keep up."
-->

---

## V2: Match in Memory, Read from Database

```
Client → FastAPI → In-Memory Engine → background flush → PostgreSQL
                   (sorted dict + queue)                  (storage)
         ↓
         Reads still go to DB
```

- Matching happens in memory: **sorted dict** for price, **deque** per price for FIFO
- Background task flushes to DB every **~30ms**
- Individual writes batched into bulk operations
- Read endpoints (order status, order book, prices, balances) still hit PostgreSQL

<!--
"I moved matching into memory. Orders are matched in microseconds. The database gets written to in the background every ~30ms."
"I used a sorted dictionary for price lookup and a queue for time priority at each price level."
-->

---

## V2: In-Memory Structures

<style scoped>
table { width: 100%; border-collapse: collapse; font-size: 28px; margin-top: 30px; }
th, td { padding: 18px 30px; text-align: left; }
th { background-color: #053A22; color: #FAFBFC; }
td { border-color: #d0d7de; }
td:first-child { font-family: 'Courier New', monospace; }
.tbl-title { background-color: #053A22; color: #FAFBFC; font-size: 24px; font-weight: bold; text-align: center; padding: 10px; }
</style>

<table>
  <tr>
    <th>Field</th>
    <th>What it stores</th>
    <th>Why in memory</th>
  </tr>
  <tr>
    <td>orders</td>
    <td>All open + recently closed orders</td>
    <td>POST /orders, /cancel</td>
  </tr>
  <tr>
    <td>book</td>
    <td>Open orders by price</td>
    <td>POST /orders (matching)</td>
  </tr>
  <tr>
    <td>brokers_by_key_hash</td>
    <td>Auth table</td>
    <td>All endpoin ts (Auth)</td>
  </tr>
</table>

---

## V2: Results

### ~75% of B3 (525 orders/second)

**Bottleneck: Database Reads**

- Read endpoints are **28% of traffic** and they all still hit PostgreSQL
- Under load, DB queries slow down — reads pile up
- Reads become the bottleneck, not writes

<!--
"Big jump, but now reads and the background writer were fighting over database connections. Under high load, the writer couldn't keep up."
-->

---

## V3: Everything in Memory, Database for Durability

```
Client → FastAPI → Full In-Memory State
                   (matching + reads + balances + prices)
                          ↓
                   background flush every ~30ms
                          ↓
                   PostgreSQL (only for recovery)
```

- All state lives in memory: order book, prices, balances, trade history
- Background task flushes changes to DB every **~30ms**
- DB only used on startup (to reload state) and as fallback for old closed orders
- Open orders + recent closed orders in memory

<!--
"I moved everything into memory. Reads, writes, matching — all in-memory. The database is there for persistence and for rare fallbacks on older closed orders."
-->

---

## V3: New In-Memory Structures

<style scoped>
table { width: 100%; border-collapse: collapse; font-size: 28px; margin-top: 30px; }
th, td { padding: 18px 30px; text-align: left; }
th { background-color: #053A22; color: #FAFBFC; }
td { border-color: #d0d7de; }
td:first-child { font-family: 'Courier New', monospace; }
.tbl-title { background-color: #053A22; color: #FAFBFC; font-size: 24px; font-weight: bold; text-align: center; padding: 10px; }
</style>

<table>
  <tr>
    <th>Field</th>
    <th>What it stores</th>
    <th>Why in memory</th>
  </tr>
  <tr>
    <td>brokers</td>
    <td>Broker info + balance</td>
    <td>GET /balance, webhooks</td>
  </tr>
  <tr>
    <td>trades_by_order</td>
    <td>Trades per order</td>
    <td>GET /orders/{id}</td>
  </tr>
  <tr>
    <td>trade_prices</td>
    <td>Last 1000 trade prices</td>
    <td>GET /stocks/.../price</td>
  </tr>
  <tr>
    <td>queue</td>
    <td>Pending DB writes</td>
    <td>Background flush to DB</td>
  </tr>
</table>

---

## V3: Results

### ~250% of B3 (1750 orders/second)

**Bottleneck: CPU (Python)**

- No database in the hot path — all operations happen in memory
- Every order still goes through one Python process, one at a time
- A single CPU core and the language Python are now the bottleneck

<!--
"It handles about 250% of B3's average order volume in the benchmark. The bottleneck is now Python itself, not the architecture."
-->

---

## Trade-offs

<style scoped>
table { font-size: 24px; }
td, th { padding: 9px 18px; }
</style>

| Trade-off | What it means | How to fix it |
|-----------|--------------|---------------|
| **Crash risk** | Lose ~30ms of data on crash (~21 orders, ~4 trades) | Write-ahead log |
| **Memory** | Full B3 day (~20M orders + 4M trades) ≈ ~14 GB | Evict completed and expired orders after flush to DB |
| **Single core** | One Python process handles all symbols | Split symbols across multiple servers |

<!--
"These are limits I chose to accept. I know how I'd solve each one."
-->

---

## What I Would Do Next

1. **Add a write-ahead log** — Log every order to disk before confirming

2. **Rewrite in Rust** — Architecture is right, language is the bottleneck

3. **Split by symbol** — Distribute symbols across servers, balanced by trading volume

<!--
These are concrete next steps, not hand-waving.
-->

---

## Summary

<style scoped>
table { font-size: 26px; }
td, th { padding: 10px 20px; }
</style>

| Version | Architecture | Capacity | Bottleneck |
|---------|-------------|----------|------------|
| **V1** | Everything in the Database | **~25%** | Database locks |
| **V2** | Match in Memory, Read from Database | **~75%** | Database reads |
| **V3** | Everything in Memory, Database for Durability | **~250%** | CPU (Python) |

<!--
"Each version taught me where the bottleneck was. By V3, the architecture wasn't the problem anymore — the language was."
-->
