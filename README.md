# Mini Stock Exchange

## 1. Goal: B3 Scale
The goal was to simulate the traffic patterns of **B3 (Brazil Stock Exchange)**.

**B3 Metrics:**
*   **Daily Trade Volume**: ~4 million trades/day (~140 trades/sec).
*   **Daily Order Volume**: ~20 million orders/day (~700 orders/sec).
*   **Scope**: ~450 symbols, ~100 brokers.
*   **Traffic**: Real mix of Limit, Market, and Cancel orders + Read requests for Order Book, Price, and Balance.

---

## 2. Results
**Max Capacity** is the percentage of B3 volume the system handles while maintaining stable latency.

| Version | Architecture | Max Capacity (% of B3) | Limiting Factor |
| :--- | :--- | :--- | :--- |
| **V1** | Database-only | **~40%** | Database Locks |
| **V2** | Memory Match / DB Reads | **~90%** | Database Reads |
| **V3** | Full In-Memory | **~300%** | CPU / Python |

---

## 3. Version 1: Database-only (40% of B3)
**Architecture:**
*   **Stateless API** + **PostgreSQL**.
*   **Logic**: Every order is a single database transaction.

**Bottleneck: Database Locks**
*   When multiple brokers trade the same symbol, the database processes them one by one (serializing).
*   **Result**: Latency spikes massively as the queue for the lock grows.

---

## 4. Version 2: In-Memory Matching (90% of B3)
**Architecture:**
*   **Matching**: Moved to **In-Memory**.
*   **Read endpoints**: Still query the database.  
*   **Persistence**: Background task saves to DB every 30ms.

**Bottleneck: Database Reads**
*   **Improvement**: Matching is now CPU-bound. Write latency drops to < 1ms.
*   **Problem**: Read endpoints (Order Book, Price) still query the database.
*   **Result**: High load on reads slows down the background writer.

---

## 5. Version 3: Full In-Memory (300% of B3)
**Architecture:**
*   **Memory**: Holds all state (Order Book, Prices, Balances).
*   **Database**: Used only for startup loading and background saving.
*   **Reads**: Served primarily from memory, with rare fallbacks to DB.

**Result: 300% Scale**
*   **Throughput**: Handles 3x B3 volume on a single node.
*   **Latency**: Stable < 1ms.
*   **Limit**: Now limited only by Python's CPU speed.

---

## 6. Architecture Details

### Data Structures
We use `SortedDict` to organize orders by price, allowing quick access to the best Bid (highest) and best Ask (lowest).
*   **Time Priority**: Inside each price level, we use a `deque` (Double-Ended Queue) to maintain First-In-First-Out (FIFO) execution.

### Asynchronous Persistence
In V2/V3, we don't write to the DB immediately.
1.  **Accumulate**: Incoming orders update the In-Memory state immediately (Response sent to user).
2.  **Queue**: We push every trade/order event into an async `Queue`.
3.  **Flush**: Every 30ms, the background task snapshots the pending changes and performs a **Bulk Insert/Update** to the DB.
*   **Benefit**: Converts thousands of small random writes into a few large sequential writes.

### Recovery
If the server crashes, we lose the last ~30ms of data (in-memory queue). At a normal trading volume, this is ~21 orders and ~3.3 trades.
*   **Mitigation**: In a real production system, we would implement a **Write-Ahead Log (WAL)** on disk to persist events *before* acknowledgment, preventing data loss even on failure.

---

## 7. Next Steps
To scale beyond 300%:
1.  **Sharding**: Split symbols across multiple servers (e.g., A-E on Server 1, F-K on Server 2, etc).
2.  **Language**: Rewrite in Rust to improve CPU performance.

---

## 8. Repository Structure
*   [**casev1/**](./casev1/README.md) — The initial database-driven implementation.
*   [**casev2/**](./casev2/README.md) — In-memory matching + DB for read endpoints and persistence.
*   [**casev3/**](./casev3/README.md) — Full in-memory architecture + DB for persistence.
*   [**bench/**](./bench/README.md) — Benchmarks testing these versions against B3 metrics.

## 9. Quick Start
To run the benchmarks and verify these results:

1.  **Pick a version** (e.g., v3):
    ```bash
    cd casev3
    docker-compose up --build -d
    ```

2.  **Run the benchmark**:
    ```bash
    cd ../bench
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    
    # Run a realistic 60s simulation at 100% B3 scale
    python perf_realistic.py --version v3 --scale 100
    ```

