# Plan — Portfolio Movement Analytics (cash-flow-aware)

Status: **approved & executed** (2026-08-24). Engine methodology `perf-v1.3`.

## Goal
Below the existing "Performance & risk" block on the portfolio analyze result,
show the portfolio's **actual value movement** reconstructed from real
purchases/redemptions (CAMS-style), marked daily with scheme NAVs — with
flow-adjusted daily returns (TWR) and the money-weighted return (XIRR).

## Decisions (confirmed by product)
1. Transactions: CAS JSON `transactions` array AND the standalone TXT envelope
   (same schema) — stored per client/actual portfolio (`transactions_json`).
2. Returns: TWR daily chain (primary) + XIRR (money-weighted), labelled.
3. Opening value deduced: `opening_units = end_units − ΣPURCH + ΣREDEEM`;
   start = earliest transaction date.
4. Display: "Portfolio movement" card below Performance & risk
   (`renderPortfolioAnalyticsBlock`, app.js:2199).
5. Honest annualization: public `annualized_twr_pct`/`xirr_pct` are null +
   dated reason when span < 90 days (`MIN_CAGR_WINDOW_DAYS` convention);
   raw formula still unit-tested; total TWR + all series always shown.
6. Sign normalization: PURCHASE/SWITCH_IN + units,+flow; REDEEM/SWITCH_OUT −.
   Source amounts are unsigned; type decides the sign.

## Phases
1. **Data**: userdata schema v3 (`client_portfolios.transactions_json`),
   `parse_cas_transactions`, ingest wiring, sample seed attach (907 recs).
2. **Engine**: `analytics.portfolio_movement_series` + attach in
   `WebDB.portfolio_analytics` (movement=None when no tx — honest).
3. **API**: `/api/portfolio-analytics` inline `transactions` or stored.
4. **UI**: movement card (chips, value+return charts, constituents table).
5. **Docs**: methodology § movement; `perf-v1.3`.
6. **Tests**: T1–T11 in `tests/test_portfolio_movement.py` + extensions;
   full suite green; deploy; live verify.

## Key formulas
- units_t = opening + Σ tx units ≤ t (step function)
- V_t = Σ_schemes units_t · NAV_t (forward-fill NAV gaps)
- F_t = Σ signed amounts on t
- daily TWR: r_t = (V_t − V_{t−1} − F_t) / V_{t−1}
- total TWR = Π(1+r_t) − 1; annualized = (1+total)^(365.25/span) − 1 (span ≥ 90d)
- XIRR: bisection on [−opening@start, F_t, +V_end@end], 365.25 basis

## Test vectors (hand-computed)
- T2: NAV 10.00 +1%/day ×5; opening 500u=5000; purchase 100u@10.303=1030.30 →
  values 5000/5050/5100.50/6181.80/6243.60; total TWR = 1.01⁴−1 = 4.0604%;
  raw annualized = (1.040604)^(365.25/4)−1; public annualized = null (4d < 90d).
- T4: 100→110 @365.25d no flows → XIRR = 10.000% exact.
- T8: real sample (907 tx) — opening ≈ Σ opening_units·NAV(start);
  value_series[-1] ≈ Σ end_units·NAV(last); tx_count sums to 907−dropped.
