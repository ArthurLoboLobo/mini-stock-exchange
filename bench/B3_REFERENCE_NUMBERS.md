# B3 Reference Numbers for `perf_realistic.py`

All values below represent 100% of estimated B3 load (`--scale 100`).

`--scale` controls both market structure and request rates. Below 100%, everything scales linearly. Above 100%, market structure (symbols, brokers) is capped at the B3 values and only rates increase — you can't add stocks that don't exist, but you can push more traffic through them.

```
--scale 25   → 112 symbols,  37 brokers,  175 orders/sec   (quick dev test)
--scale 100  → 450 symbols, 100 brokers,  700 orders/sec   (realistic B3)
--scale 300  → 450 symbols, 100 brokers, 2,100 orders/sec  (stress test)
```

## Constraints

### Market structure

```python
B3_SYMBOLS = 450
B3_BROKERS = 100
ZIPF_EXPONENT = 0.95
```

### Write traffic

```python
B3_ORDER_RATE = 700     # new orders/sec (B3 estimated average)
B3_CANCEL_RATE = 245    # cancels/sec (~35% of new orders)
# Total write rate: ~945/sec
```

### New order mix

```python
PASSIVE_LIMIT_PCT = 0.75     # won't match immediately
AGGRESSIVE_LIMIT_PCT = 0.20  # priced to cross the spread
MARKET_ORDER_PCT = 0.05      # IOC
```

### Cancel timing (delay after original order)

```python
CANCEL_FAST_PCT = 0.40    # 0.5–5 sec
CANCEL_MEDIUM_PCT = 0.35  # 5–60 sec
CANCEL_SLOW_PCT = 0.25    # 1–10 min
```

### Read traffic

```python
READ_MIX = 0.28  # 28% of all requests are reads

READ_ORDER_STATUS_PCT = 0.35  # GET /orders/{id}
READ_PRICE_PCT = 0.30         # GET /stocks/{symbol}/price
READ_BOOK_PCT = 0.25          # GET /stocks/{symbol}/book
READ_BALANCE_PCT = 0.10       # GET /balance
```

### Order duration (`valid_until`)

```python
DURATION_VERY_SHORT_PCT = 0.10  # 0.5–1 sec (HFT/scalping quotes)
DURATION_SHORT_PCT = 0.20       # 5–10 sec (algo/market-maker requotes)
DURATION_DAY_PCT = 0.70         # 1 day (retail + institutional, effectively never expires)
```

### Scale behavior

```markdown
- **Market Structure:** Symbols and brokers scale linearly up to 100%, capping at 450 and 100 respectively.
- **Request Rates:** Order and cancel rates scale linearly with the scale percentage, with no upper cap.
- **Floors:** Minimum values are enforced (5 symbols, 3 brokers, 10 orders/sec, 5 cancels/sec) to ensure the test remains functional at very low scales.
```

---

## Justifications

### Trades per day

- ~4M trades/day average, from the B3 Boletim Diário do Mercado ([source](https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/consultas/boletim-diario/boletim-diario-do-mercado/)).

### Order and cancel rates

- With a 5:1 order-to-trade ratio: 4M trades × 5 = 20M new orders/day. The 5:1 ratio is conservative for a low-HFT market — high-HFT venues (US, EU) see 10–30:1; B3's low HFT penetration (~10–15%) brings it well below that range.
- B3's equity market is open for ~8 hours/day (28800 trading seconds).
- 20M ÷ 28800 ≈ 700 new orders/sec average.
- ~35% cancel rate, reflects B3's low HFT penetration and single-venue structure (no fragmentation-driven cancel inflation).

### New order mix and match rate

- 25% of new orders are priced to match immediately (20% aggressive limit + 5% market). Not all will find counterparties — actual fill rate is lower.
- Some orders are cancelled after being partially or fully filled. The 25% aggressive rate and 35% cancel rate can overlap.
- Market orders are 5% — retail uses them heavily but institutional/algo participants almost exclusively use limits.

### Cancel timing

- On NASDAQ, 90% of cancels happen within 1 second. Retail orders average >20 min before cancel.
- B3 has less HFT than NASDAQ, so the distribution shifts toward slower cancels.
- Note: the 25% "slow" cancels (1–10 min) mean the full cancel rate only materializes after several minutes. In a 60s benchmark, the effective cancel rate will be lower during ramp-up.

### Read traffic

- Real exchange gateways are write-dominated — market data is typically served via a separate feed, not the order entry API. Our API serves both, but reads are a minority of traffic (~28%).

### Symbol distribution

- B3 concentration: top 5 stocks = 25–30%, top 10 = 35–40%, VALE3 alone ~10%.
- s=0.95 produces top 1 ~11%, top 10 ~40%, matching B3.

### Market structure

- B3 has ~450 listed stocks and ~100 registered broker-dealers.

### Order duration

- 70% are day orders (broker default for retail and institutional).
- 20% are short-lived algo/market-maker quotes (5–10 sec), creating realistic book churn.
- 10% are very short HFT/scalping quotes (0.5–1 sec), stress-testing the expiration path.

### Scale behavior

- Below `--scale 100`, everything scales down proportionally — useful for quick dev tests with fewer symbols and brokers.
- Above `--scale 100`, market structure is capped at B3 values (450 symbols, 100 brokers) and only request rates increase. This models stress scenarios: same market, more traffic per symbol.
- Estimated B3 peak (~2,275/sec) is reachable at `--scale 325`.
