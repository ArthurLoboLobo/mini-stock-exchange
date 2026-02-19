# Mini Stock Exchange — Benchmark Suite

Benchmark suite for the Mini Stock Exchange. Tests correctness, measures per-operation latency, and simulates realistic trading load (B3).

## 1. Overview & Design

The suite is split into three distinct tools:

*   **`correctness.py`** (Functional Validation): Validates that the matching engine follows trading rules (e.g., price-time priority, self-match prevention). Tests are **untimed** to rigorously check logic without flakiness.
*   **`perf_micro.py`** (Component Isolation): Measures the cost of specific operations in isolation. Instead of simple point metrics, it generates **scaling curves** (e.g., O(N) vs O(log N)) to reveal algorithmic bottlenecks in book depth, partial fills, and concurrency.
*   **`perf_realistic.py`** (Production Simulation): Simulates a real trading day. Uses **open-loop dispatch** (requests sent on schedule regardless of server speed) with **Poisson arrivals** and **Zipf symbol distribution** to model realistic traffic bursts and hotspot contention. This reveals true tail latency under load.

## 2. Setup

**Prerequisites**: Python 3.11+, Docker, and the exchange API running at `http://localhost:8000`.

```bash
cd bench
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Usage

### Correctness Tests
Validates logic without timing pressure. Should pass for all versions.
```bash
python correctness.py --version v1
```

### Micro-Benchmarks
Measures isolated operation costs and algorithmic scaling.
```bash
python perf_micro.py --version v1
```

### Realistic Benchmark
Simulates a 60s trading window.
```bash
# Default: 25% B3 scale
python perf_realistic.py --version v1

# Full B3 scale (100%)
python perf_realistic.py --version v1 --scale 100
```

## 4. Full Workflow (V1 vs V2 vs V3)

To benchmark all versions:

```bash
# Run V1
cd ../casev1 && docker-compose down -v && docker-compose up --build -d
cd ../bench
python perf_realistic.py --version v1 --scale 100

# Run V2
cd ../casev2 && docker-compose down -v && docker-compose up --build -d
cd ../bench
python perf_realistic.py --version v2 --scale 100

# Run V3
cd ../casev3 && docker-compose down -v && docker-compose up --build -d
cd ../bench
python perf_realistic.py --version v3 --scale 100
```

## 5. Persistence & Results

*   **Logic**: Tests use API-only interactions for portability.
*   **Output**: Results are saved as JSON in `bench/results/` (e.g., `v1_realistic_latest.json`).
*   **Analysis**: Check raw JSONs to compare versions.
