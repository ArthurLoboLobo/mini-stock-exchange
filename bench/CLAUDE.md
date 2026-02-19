# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Benchmark suite for the Mini Stock Exchange (V1 and V2). Tests correctness of matching rules, measures per-operation latency (micro-benchmarks), and simulates realistic trading load with Poisson arrivals. Runs against the exchange API over HTTP — does not import application code.

## Prerequisites

- Python 3.11+
- The exchange API running locally at `http://localhost:8000` (start via `docker-compose up --build` in `casev1/` or `casev2/`)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies: `httpx`, `aiohttp`, `uvloop` (non-Windows).

## Commands

```bash
# Correctness tests (pass/fail, no timing)
python correctness.py --version v1

# Micro-benchmarks (per-operation latencies, scaling curves)
python perf_micro.py --version v1

# Realistic load test (Poisson arrivals, configurable scale + duration)
python perf_realistic.py --version v1

```

Results are saved as JSON in `results/` (timestamped + `_latest.json` symlink).

## Code Layout

- `shared.py` — Common utilities: constants (`DEFAULT_URL`, `DEFAULT_ADMIN_KEY`), ANSI colors, order payload builders (`limit_order`, `market_order`), auth helpers, `WebhookSink` (threaded HTTP server for capturing webhooks), `timed_request`, `register_brokers`, `reset_db`, `BenchmarkResult` (percentile calculator), `save_results`, `poll` (for V2 eventual consistency)
- `correctness.py` — 15 pass/fail tests validating matching rules (price-time priority, partial fills, market orders, expiration, cancel orders, stock price queries, etc.)
- `perf_micro.py` — Isolated operation latency: single order (no match), single match, read, book query. Scaling curves for book depth, partial fills, and concurrency.
- `perf_realistic.py` — Simulated trading window with open-loop Poisson dispatch, Zipf symbol distribution, coordinated omission correction, cancel orders with async Future-based order ID resolution, and symbol-based worker assignment. B3-calibrated constants (450 symbols, 150 brokers, 700 orders/sec, 245 cancels/sec). Uses `aiohttp` + `uvloop` + multiprocessing to avoid client-side bottlenecks. Configurable via `--scale`, `--duration`, `--workers`.
- `results/` — Output directory for JSON results (gitignored)

## Key Design Decisions

- **Open-loop dispatch**: Poisson inter-arrival times model real traffic (avoids coordinated omission)
- **Coordinated omission correction**: Honest tail latency under overload
- **Zipf symbol distribution**: Creates realistic hotspot contention on popular tickers
- **WebhookSink**: Threaded HTTP server that captures webhook POSTs during benchmarks, accessible via `host.docker.internal`

## Configuration

All scripts accept `--url` (default: `http://localhost:8000`) and `--admin-key` (default: `admin-secret-key-temporary`). Correctness and perf scripts require `--version` (e.g., v1, v2). The realistic benchmark additionally accepts `--scale` (% of B3 load), `--duration` (seconds), `--no-reads`, `--workers`, and `--seed`. Symbols and brokers are derived from `--scale` via `scale_params()`. See `TUTORIAL.md` for full CLI reference.
