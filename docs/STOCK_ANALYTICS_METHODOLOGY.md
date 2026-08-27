# Stock Analytics Methodology — technical · factors · statements · fundamentals

Living reference for every figure the stock-side engines emit. Sibling to
`ANALYTICS_METHODOLOGY.md` (mutual-fund performance engine) and governed by
the same house rules: honest nulls, as-of = last data point, no advice,
no performance claims.

Version stamps (each payload carries its own):
- Technical: **`tech-v1.0.0`** (`webapp/stock_technical.py`)
- Factors:   **`factor-v1.0.0`** (`webapp/factor_scores.py`)
- Statements:**`stmt-v1.0.0`** (`src/statement_schema.py`)
- Fundamentals:**`fund-v1.1.0`** (`webapp/stock_fundamental.py`)
- Statement table:**`fund-table-v1.1.0`** (annual/cash-flow grid, §4)

## 1. Technical engine [tech-v1.0.0]

Input: daily OHLCV arrays from `data/stock_history/<ISIN>.json`
(NSE bhavcopy 2020→ present; Yahoo close-only bars before that — those
bars carry `None` for O/H/L/V and price-only indicators run while
H/L/volume indicators honestly degrade).

Conventions: Wilder smoothing for RSI/ATR/ADX; EMA seeded with SMA(n);
Bollinger uses population sigma; ATR's first value lands n bars after the
first valid TR (bar-0 has no previous close); Supertrend seeds bullish;
StochRSI returns neutral 50 when its window is flat.

Composite score = 0.40·trend + 0.35·momentum + 0.25·volume, each component
the mean of ±1 indicator votes (trend) or clipped normalised readings
(momentum/volume). Bias: ≥+25 bullish, ≤−25 bearish, else neutral.

Signals: SMA50×200 golden/death cross, MACD×signal crosses, RSI 30/70 zone
exits, Supertrend flips, regular RSI divergences (strict fractal pivots,
k=5). Patterns: 13 OHLC-geometry candlesticks over trailing 250 bars.
Anchors: `tests/test_stock_technical.py` (StockCharts RSI example 70.46,
hand-computed Bollinger/ATR/pivots/fibs/Ulcer, etc.).

## 2. Factor platform [factor-v1.0.0]

Universe: securities with `confirmed_equity=1` AND ≥180 trailing closes
(857 of 868 at build time). Components → winsorized z (±3) → percentile
rank 0–100 via strictly-below/(n−1) so ranks span the full range; ties share
a rank. Inverted components (vol_252, downside_dev, beta, max_drawdown,
accruals, leverage, ev_ebitda) flip sign BEFORE z-scoring.

Factor composites need ≥3 available component ranks (MIN_COMPONENTS);
multi-factor needs ≥2 factor composites. Sector-relative momentum/low-vol
ranks computed within sector buckets (≥5 names each).

Momentum: classic skip-month windows — 12−1 / 6−1 / 3−1 (window ends 21
trading days back) + risk-adjusted 12−1 ÷ annualised σ. Low-vol: 252-bar
σ, downside deviation, OLS beta vs NIFTY 500 TR on common dates, max-DD.
Value/quality activate per-stock once statements exist (see §3); until
then they are honest nulls universe-wide.

No backtests, no baskets, no performance claims — descriptive ranks only.
Validation mode may ALIGN our ranked baskets against the on-disk NIFTY
factor-index TR series descriptively.

## 3. Financial statements pipeline [stmt-v1.0.0]

Source policy unchanged: **NSE only**. `stock_reports.py` announcement URLs
are deep-fetched (financial-results headlines only), PDFs downloaded to
`data/raw/financial_results/<SYMBOL>/`, then extracted:

1. **AI vision tier (primary)** — pages chosen by text-layer keyword score
   (image-only scan pages qualify blindly), one OpenRouter vision call per
   page (default `google/gemini-2.5-flash`, temperature 0, JSON mode),
   strict contract `{sections:[{section, unit, periods[], rows[]}]}`
   with values-array-length == periods-length. Per-PAGE cache keyed by
   file sha256 + PROMPT_VERSION — successes never re-bill, failures never
   erase sibling pages. Disable with `STMT_AI=0`; override model with
   `STMT_AI_MODEL`.
2. **Deterministic tier (fallback)** — pdfplumber word positions:
   header band detection (≤3 lines, extended while fresh dates appear),
   monotonic min-distance DP alignment of figure-column x-centers onto
   header anchors, Reg-33 canonical slot sequence anchored at the
   headline-declared period (`_canonical_slots`).
3. Both tiers emit identical raw rows; labels map through the Schedule III
   alias dictionary (`match_label`, exact > fuzzy ≥0.72) and expense-family
   lines are stored positive. Derived keys (EBITDA = TI − TE + Dep + Fin −
   exceptional; total_debt = LT + ST; net_worth; total_liabilities = A − E)
   fill when inputs allow.

Periods: cumulative columns (H1/9M/FY) derive discrete quarters by chain
subtraction inside each Apr–Mar fiscal year; TTM sums the last four
DISCRETE quarters (contiguity gate ~3 months) and never sums EPS/DPS.
Units normalised to ₹ crore at ingestion (per-share items stay rupees).

Validation gate: Assets ≈ Equity + Liabilities (±0.5%, only when L parsed),
revenue non-negative, PAT ≤ 3×PBT sanity, expenses sanity; failures flag
`needs_review`-style issues and cut confidence — nothing is silently zeroed.
Storage: `data/stock_financials/<ISIN>.json` with standalone/consolidated
blocks (consolidated preferred downstream), per-source sha256 traceability.

Coverage reality (measured): digital PDFs parse near-perfectly; pure scans
(HDFC Bank) rely entirely on the AI tier; some filings omit EPS rows —
shares then come from paid-up capital ÷ face value (captured from the
label), and EPS is derived as PAT/shares and flagged `eps_derived`.

## 4. Fundamentals engine [fund-v1.1.0]

Pure functions over the statement document + latest close. TTM block drives
current ratios; balance-sheet fields merge from the latest audited annual
(TTM carries only flow items). Shares outstanding: EPS bridge first
(PAT/basic-EPS), capital÷FV fallback. ROE/ROA average current & prior-year
denominators when both exist.

Families (~50 metrics): profitability (gross/EBITDA/operating/net margins,
ROE, ROA, ROCE, ROIC with effective tax), DuPont 3-way (+5-way tax &
interest burdens), liquidity (current/quick/cash/WC-sales), leverage
(D/E, debt & net-debt ÷ EBITDA, interest coverage = EBIT/finance-cost),
efficiency (turnovers + inventory/receivable/payable days + CCC),
cash-flow quality (FCF = CFO − capex, FCF margin, OCF/PAT accrual check,
capex/sales, FCF/EBITDA), growth (YoY + up-to-3y CAGRs on the 365.25-free
trading-period basis, positive-year consistency), valuation (P/E, P/B,
P/S, P/CF, EV/EBITDA, dividend yield, earnings yield, PEG vs revenue CAGR,
Graham number). Negative/degenerate denominators → honest null (negative
EPS never yields a P/E).

Composite scores: Piotroski F (9 binary tests, n/a when inputs missing,
score counts evaluable only), Altman Z public-firm weights (zones <1.81
distress / <2.99 grey / ≥ safe; TL falls back to A − E), Beneish M
8-variable index with M > −1.78 flagged as a statistical screen — a review
prompt, never an accusation. Anchors: `tests/test_stock_fundamental.py`.

### 4.1 Annual statement table [fund-table-v1.1.0]

`build_annual_table(doc)` renders the three statements as **rows, audited
fiscal years as columns** (a single grid per user requirement). Visible in
the Statements tab via `GET /api/securities/{isin}/financials/table`.

Columns = **last ≤5 distinct audited fiscal years** present in the document,
ascending (based on period-end), so 1–5 columns in practice — the actual
coverage at build time is dominated by FY22–FY24 (528 of 673 docs). Years
are never fabricated: less coverage means fewer columns, never interpolated
figures.

Row catalog (`_TABLE_ROWS`, grouped Income statement → Balance sheet → Cash
flow) then computed metrics (`_METRIC_ROWS`); ~25 rows, ₹ crore floats, per-share
items marked ₹. Computation rules reused from the fundamentals families:
margins = /revenue; ROE/ROA average previous+current equity/assets when both
exist; `FCF = CFO − capex`; `EBITDA = TI − TE + Dep + Fin − exceptional`;
DPS 0 only when a dividend line parsed; denominators ≤0 → honest null.

Per-year metrics include gross/EBITDA/operating/net margins, ROE, ROA, D/E,
current ratio, interest coverage, FCF, YoY growth.

Multi-year block (only for ≥2 years, else honest nulls):
- **CAGR** on revenue, PAT, EPS, CFO (same trading-period basis as §4
  growth; `span = n − 1`);
- **Averages & consistency**: mean/median margins, mean ROE/ROA/D/E/current
  ratio, positive-PAT ratio with loss-year labels, cumulative FCF, net-debt
  change (first → last year);
- **Volatility of growth**: σ of revenue/PAT YoY growth (pp) + coefficient of
  variation (σ/|mean| × 100, which stays meaningful on lower growth);
- **Trend** via simple OLS slope on the last up-to-5 observations:
  improving (sign > ~slope-equivalent threshold), declining, flat.

Arithmetic checks (`checks`) recompute each statement independently and emit
pass / fail / not-applicable (tolerances in the payload's assumptions):
assets = equity + liabilities (±0.5%), EBITDA bridge (from recorded T-format
lines, ±1%), **quarter-sum cross-check** (sum of discrete quarters within a
FY vs the audited annual revenue, ±2% — a data-quality canary: a handful of
docs (≈6%) trip it on upstream parse quirks like an annual figure pulled from
a different segment while the audited tables themselves still satisfy every
other check), YoY chain compounding into the total growth (±1%), and net
margin = PAT/revenue identity.

Warnings (`warnings`) surface honest gaps: balance-sheet / cash-flow items
missing for a fiscal year, loss years, single-year docs, and large TTM-vs-
latest-audited margin divergence. Every figure stays null when its inputs are
absent — nothing is guessed.

Assumptions shipped in the payload are the machine-readable copy of what is
documented here (audited XBRL/PDF figures, ₹ crore, NSE-only, best-available
of consolidated vs standalone, traders may restate, etc.).

## 5. Serving & parity

Webapp endpoints: `/api/securities/{isin}/technical | financials |
financials/table | fundamentals | factors`, `/api/factors/universe`,
`/api/factors/screen`.
Security drawer renders them under lazy-loaded tabs; every surface shows
the not-advice disclaimer. Chatapp vendors the engines via
`chatapp/scripts/sync_from_parent.ps1` and exposes `stock_technicals` +
`company_fundamentals` tools; perf-engine files keep their GENERATED stamp
(enforced by chatapp `test_perf_parity.py`). Scheduler: weekly stale-first
refresh (`statements_refresh`, STALE_DAYS=35, top-held priority).

## 6. Guardrails

Figures are factual computations over public NSE/Yahoo data and filed
accounts. Signals/scores/ranks are arithmetic descriptions of the past —
they are not recommendations, targets or forecasts. Every payload carries
its methodology version and as-of date; renderers must keep disclaimers
attached. Cache keys include methodology versions so math changes
invalidate stored results.
