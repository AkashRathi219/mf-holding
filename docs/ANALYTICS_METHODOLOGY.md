# Analytics Methodology — calculation engine & data handling

Living reference for every figure the performance engine emits
(`webapp/analytics.py`, served by `webapp/db.py`, rendered by
`webapp/static/js/app.js`). Guarded by hand-computed tests in
`tests/test_analytics.py`, `tests/test_compare.py`,
`tests/test_portfolio_analytics.py`.

Methodology version: **`perf-v1.4-2026-08-24`** (stamped into every
`compute_series_analytics` payload as `methodology_version`, and into
proposals).

---

## 1. Conventions

| Convention | Value | Why |
|---|---|---|
| As-of date | **last NAV date in the series**, never wall-clock today | a figure must never claim freshness its data doesn't have |
| Annualisation (daily stats) | × √252 | trading-day convention for vol/TE/alpha |
| CAGR exponent | `365.25 / observed_days` | calendar-day convention, leap-safe |
| Risk-free rate | 6.0 % p.a. (`DEFAULT_RF_PCT`, env `ANALYTICS_RF_PCT` override) | documented assumption until a T-bill feed lands |
| Rounding | percentages 2 dp, Sharpe/Sortino/beta/IR 3 dp | display-level precision only; math is full precision |
| Null policy | **honest nulls** — a metric with insufficient history is `null` → rendered `—`, never zero, never a since-inception figure in a 3Y slot | the core product rule |
| Units | percentages are ×100 of fractions at the API boundary | engine returns fractions internally |

## 2. Thresholds (the honest-null gates)

| Gate | Constant | Meaning |
|---|---|---|
| `MIN_POINTS_FOR_STATS = 30` | analytics.py | <30 usable points in a window → no risk stats |
| `MIN_CAGR_WINDOW_DAYS = 90` | analytics.py | <90 calendar days of history → even since-inception CAGR is null (annualising a few days fabricates absurd rates) |
| window completeness | `days ≥ round(years·365.25) − 5` | a 1Y/3Y/5Y CAGR is null unless the observed span covers the full requested window (−5-day calendar tolerance); a young fund never shows its SI figure as a "3Y" number |
| `MIN_RISK_WINDOW_DAYS = 365` | analytics.py | risk stats additionally require the recent 3y slice to **span** ≥365 days — ≥30 points over 5 weeks must never masquerade as "3-year" volatility |
| rolling-1Y floors | ≥30 usable points **and** ≥30 computed windows | else the distribution is null |
| benchmark stats | ≥30 common days **and** ≥365-day common span | beta/alpha/TE/IR need a real overlap |
| compare/portfolio common window | ≥90 days | else `window.common = false` and growth series are flagged |
| chart minimums (frontend) | >30 weekly rolling points; >11 growth points; ≥2 canvas points | charts.js hides under-drawn series rather than implying a trend |

Every gate failure is **explicit**: `risk: null` plus a machine-readable
`risk_unavailable` block (see §5) so the UI can state which dates were
considered and what was found.

## 3. Data assembly (how a series reaches the engine)

```
AMFI NAV history (90-day chunked backfill, src/nav_history.py)
        └─> data/nav_history/<amfi_code>.json   (full since-inception, oldest-first)
mfapi mirror (src/fetch_missing_nav.py)          (full history for codes the CSV missed, e.g. ETFs)
daily appends (src/nav_daily.py)                 (merges last N days; NEVER seeds thin stubs — §7)
R2 object store (deploy/upload_r2.py)            (curated full-history set; lazy-pulled per request)
        └─> webapp/db._load_nav_plan(code)       (read path; heals thin files — §7)
                └─> compute_series_analytics([(date, nav), ...])
```

Read-path selection (`db.scheme_analytics`): Direct plan preferred, else
Regular. Benchmark selection v2: (1) the scheme's own tracked index
(`schemes.index_name`) when populated — strongest signal; (2) ordered
word-boundary keyword rules over category+fund name, most-specific first
("nifty 500" before "nifty 50", factor names with numbers before plain
factors — full table in `webapp/db.py::_BENCHMARK_RULES`, mapping tests in
`tests/test_benchmark_mapping.py`); (3) equity-only default NIFTY 500.
Every mapped index has a TR series in `data/nifty/TR/`; a missing series
degrades to a `"<index> (series unavailable)"` label, never a wrong number.

**Deferred decisions (data acquisition, not code):**
- *Debt/hybrid composite benchmarks*: no debt TR series exists in the repo;
  equity-only mapping stays until one is sourced.
- *Risk-free rate*: still the documented 6.0% assumption until a T-bill
  feed lands.

## 4. Date-handling operations (in order)

1. **Parse** `parse_nav_date()`: accepts `DD-Mon-YYYY`, ISO `YYYY-MM-DD`,
   `date`/`datetime`; anything else → dropped (never guessed).
2. **Dedupe**: same date seen twice → **last value wins** (AMFI revisions).
3. **Drop non-positive NAVs**: `v ≤ 0` points are unusable, not zero.
4. **Sort** chronologically.
5. **As-of** = last sorted date.
6. **Window cut-offs** are calendar-day subtractions from the as-of date:
   `today − round(years × 365.25)`.
7. **Tolerances**: window completeness and rolling windows allow −5 days of
   calendar gaps; the chart-level rolling scan requires base-point gaps
   ≥355 days.
8. **Chain breaks**: `daily_returns()` skips a pair when either side is ≤0 —
   a single 0-value point breaks the chain rather than producing −100%.

Every emitted metric group carries its own dates (§5), and the UI renders
them beside each figure.

## 5. Metric definitions & window disclosure

Each `*_window` object: `{start, end, days, points, complete}` (ISO dates).
`complete: false` means the fund is younger than the requested window — the
value is null and the dates explain why.

### CAGR `cagr_pct`
- `since_inception`: `(last/first)^(365.25/span_days) − 1`, gated by the
  90-day floor. Window = full record.
- `y1 / y3 / y5`: same formula over `today − N·365.25` → as-of, gated by
  full-window coverage. Worked example (SBI Nifty 50 ETF, as of 2026-08-21,
  2,744 points since 2015-07-23): SI **10.57%** (4,047 days), 1Y **−2.32%**
  (2025-08-21 → 2026-08-21, 253 pts), 3Y **8.92%** (753 pts), 5Y **9.24%**.

### Risk block `risk` (3y slice; needs ≥30 pts AND ≥365-day span)
- Daily simple returns over the slice.
- `volatility_pct` = sample-std(daily) × √252 × 100.
- `sharpe` = (3Y-window CAGR − rf) / vol_ann. The 3Y CAGR (not SI) is the
  return input so numerator and denominator describe the same period; when
  the 3Y window isn't covered the CAGR is 0 → Sharpe honestly null or very
  negative rather than fabricated.
- `sortino` = (3Y CAGR − rf) / (downside-deviation × √252); downside dev =
  √mean(min(r − rf/252, 0)²).
- `max_drawdown_pct` = min over the slice of `v/running_peak − 1` (0.0 for a
  monotonic rise; null only if no positive value ever seen).
- Emits `window_start`, `window_end`, `n_points`.
- Worked example (same ETF): window 2023-08-21 → 2026-08-21, 753 pts,
  vol **13.13%**, Sharpe **0.223** ((8.92−6)/13.13), Sortino **0.311**,
  MaxDD **−15.45%**.

### `risk_unavailable` (when the gates fail)
`{reason, required_points, required_span_days, window_start, window_end,
found_points, found_start, found_end}` — e.g. the cold-start stub case:
"needs ≥30 points spanning ≥365 days inside 2023-08-21 → 2026-08-21; found 5
(2026-08-17 → 2026-08-21)". The UI prints this verbatim-ish instead of a
bare "not enough history".

### Rolling 1Y distribution `rolling_1y`
365-day windows at daily steps over the **full** record; a window counts when
its base-point gap ≥360 days; needs ≥30 windows. Emits `window_days`,
`first_window_start`, `last_window_end`, `n_periods`, `pct_positive`, best /
worst / median. Worked example: 2,503 windows, **88.4% positive**.

### Benchmark-relative `benchmark` (needs ≥30 common days AND ≥365-day span)
Only dates present in **both** return maps count.
- `beta` = cov(r_s, r_b) / var(r_b)
- `alpha_pct` = (mean_s − β·mean_b) × 252 × 100  (Jensen's, annualised)
- `tracking_error_pct` = std(r_s − r_b) × √252 × 100
- `information_ratio` = (mean_s − mean_b)×252 / TE (null when TE = 0)
- Emits `window_start`, `window_end`, `n_days`.

### Portfolio & compare layers
- `portfolio_analytics`: weights normalised to 100, blended on the densest
  constituent's grid with forward-fill, growth rebased to ₹100 at the common
  start; common window = max(starts)…min(ends), flagged `common` when ≥90 d.
- `compare_schemes`: same common-window rule; per-scheme metrics computed on
  each scheme's own full record (never truncated to a bad common window).

### Portfolio movement (`movement`, [ANA3]) — cash-flow-aware
Distinct from the weight-blended index: this reconstructs the investor's
ACTUAL money path from the statement's purchases/redemptions
(`tools_api.parse_cas_transactions`; stored per client portfolio in
`transactions_json`).
1. **Opening deduction**: `opening_units = end_units − ΣPURCH + ΣREDEEM` per
   scheme; start = earliest transaction date; end = max(last tx date, last
   framed NAV). End units come from the statement's allocations
   (`net_units` on the items).
2. **Daily valuation grid** = union of scheme NAV dates and tx dates; units
   step function (`opening + Σ signed tx units ≤ t`), value = units × NAV
   (gaps forward-filled); `V_t = Σ scheme values`; days with no valued
   constituent are skipped.
3. **Daily TWR (flow-adjusted)**: `r_t = (V_t − V_{t−1} − F_t) / V_{t−1}`
   where `F_t` = signed amounts dated since the previous grid day
   (PURCHASE/SWITCH_IN +, REDEEM/SWITCH_OUT −; source amounts are unsigned,
   the TYPE decides the sign, and `total_net_flow` is the signed sum).
4. **Till-date numbers**: `total_twr_pct = Π(1+r_t) − 1` (always shown);
   `annualized_twr_pct` and `xirr_pct` are only emitted when the observed
   span ≥ `MIN_CAGR_WINDOW_DAYS` (90) — below that, honest nulls plus
   `annualized_unavailable {span_days, required_span_days, window}` — the
   same honest-nulls rule as single-scheme CAGRs.
5. **XIRR** (money-weighted): bisection on dated flows
   `[−opening_value@start, ±tx amounts, +terminal_value@end]`, 365.25 basis.
6. `max_drawdown_pct` is computed on the flow-adjusted linked index, not the
   raw value path (cash flows would otherwise manufacture drawdowns).
7. Constituents report opening/end units, tx counts and first/last tx dates;
   schemes without NAV history are honestly omitted (`nav_missing` class).
   No transactions → `movement: null` (weight-blend block always still runs).

**Cash semantics (perf-v1.4):** `parse_cas_transactions` signs every amount
by type — the engine consumes them as-is (no double signing). SWITCH_IN /
SWITCH_OUT are internal transfers: excluded from units, flows and invested
(reported in `data_note.switches_skipped`). `total_net_flow` = real-cash
purchases − redemptions; `cash_in` / `cash_out` expose the gross pair;
`recon` echoes opening value at series start, opening units, flow sum and
terminal so every figure reconciles. Series-start rule: a full-history
statement (deduced opening ≈ 0) begins at the FIRST PURCHASE date; a partial
statement (positive deduced opening) keeps the earliest valuable NAV date
and sets `data_note.partial_statement` — the live card flags this honestly
instead of silently minting a fabricated start.

**Known limitation (documented, honest):** for statements whose transaction
list is PARTIAL — the deduction `opening = end − Σtx` then assigns the
remainder to the EARLIEST available NAV date (often the fund's inception),
not the account's true first-holding date. Full-history statements (the seed
sample) produce opening ≈ 0 and a clean path; partial uploads can show an
inflated outset value and a correspondingly noisy XIRR. Days whose
flow-adjusted return exceeds ±99% (statement amount/units inconsistencies)
are **excluded from the geometric chain and reported** via
`data_note.artifact_days`; when any exist, `max_drawdown_pct` is null with
`drawdown_unavailable` instead of a manufactured −100%.

Example: TWR annualised of a 10.5-year 907-transaction sample (Client 1
seeded portfolio) is computed once till date — no 1Y/3Y/5Y slices.

### Debt duration `[DBT5]`
`bullet_modified_duration`: zero-coupon closed form (`ModDur = T/(1+y)`);
coupon-bearing via periodic PV schedule + bisection YTM solve. Hand-computed
anchors: ZC 5y @6% → 4.7170; 1y 8% par → 0.9259; 2y 10% @8% → 1.7691.

## 6. UI contract (dates beside every figure)

- Scheme Details: each CAGR cell carries `start → end (+ partial)`; the risk
  block opens with a "Window (risk)" row; rolling and benchmark rows carry
  their spans; the footer shows as-of, inception, point count, plan and
  methodology version.
- Compare: every metric cell carries that scheme's own window dates.
- Portfolio analytics: same cell sub-labels + window rows.
- Empty states quote the considered dates and what was found, from
  `risk_unavailable`.

## 7. Data-handling postmortem: cold-start stubs [NAV-STUB]

**Incident** (2026-08-24): a fresh Railway deploy booted with an empty
`data/nav_history/`. The scheduled `nav_daily` run found ~3,394 universe
codes without files and seeded each with a 5-trading-day stub
(`window: 2026-08-16..2026-08-23`, `created: 3394`). Because
`remote_store.ensure()` treats an **existing** local file as authoritative
and never re-fetches, those stubs permanently shadowed the full R2
histories. SBI Nifty 50 ETF (AMFI 135106, full 2,744-point history on R2)
served 5 points → analytics honestly returned nulls → the Scheme Details
screen showed "Not enough NAV history for analytics yet."

**Fixes (all three layers):**
1. `src/nav_daily.py` — missing files are filled with FULL histories only
   (R2 object first, mfapi mirror under a 100/run cap); thin stubs are never
   written; unfilled codes stay absent so the read path can fetch from R2.
2. `webapp/db.py:_load_nav_plan` — heal-on-read: a local file <30 points
   triggers ONE upgrade attempt per code per process (R2 re-download kept
   only when it has more points, else one mfapi mirror fetch).
3. `scripts/audit_nav_history.py` — sweeps all scheme codes, classifies
   ok/stub/no_file, `--heal` upgrades stubs in place.

**Lesson**: a partial cache is worse than no cache. Absence is honest and
self-healing; a thin file lies forever.

## 8. Version history

| Version | Date | Change |
|---|---|---|
| perf-v1.0-2026-08-23 | 2026-08-23 | Initial engine [ANA1]: CAGRs, risk block, rolling-1Y, benchmark stats, duration [DBT5] |
| perf-v1.1-2026-08-24 | 2026-08-24 | Risk/benchmark span guards (≥365d); `*_window` date objects on every metric group; `risk_unavailable` reason block; `methodology_version` stamp; [NAV-STUB] pipeline fixes |
| perf-v1.2-2026-08-24 | 2026-08-24 | Benchmark selection v2 (index_name + ordered keyword rules — a Nifty 50 ETF now benchmarks NIFTY 50, not NIFTY 500); per-scheme rolling-1Y series + history-completeness badge in the payload; module-level analytics cache; daily stub pre-heal job; data_health stub-shadow component; trigger-mode telemetry labels; scheme-code resolver (human-review CSV) |
| perf-v1.3-2026-08-24 | 2026-08-24 | [ANA3] Portfolio movement: cash-flow-aware value path (opening deduction, daily grid valuation, flow-adjusted TWR chain, XIRR, linked-index drawdown) attached to portfolio analytics when transactions exist; honest 90-day annualisation floor; CAS transaction ingest + per-portfolio storage + seed demo |
