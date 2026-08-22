# Analysis Results — calculation

Running a client portfolio (or model) against a strategy produces a **compliance report**:
each rule is checked (limit vs actual), an overall compliance % is computed, and charts
show asset / cap / sector allocation, top holdings and debt analytics.

## How it is calculated

`POST /api/analyze` (`webapp/tools_api.py::_analyze`) with `{portfolio_id, kind}` or
`{items, strategy_id}`:

1. **Resolve the portfolio** to either:
   - **`items`** (actual holdings) → `WebDB.portfolio_analysis(items)` expands every
     scheme into its underlying securities (weight × %NAV) and adds direct stocks.
   - **`allocations`** (model target) → `_analyze_allocations` builds metrics straight
     from the asset/cap plan (no security-level data).

2. **Build metrics** used by the rules:

   | Metric | Source |
   |---|---|
   | `asset_split` | Equity/Debt/Gold/International/Cash weights (raw % of portfolio) |
   | `cap_split` | Large/Mid/Small/Micro weights |
   | `single_stock_max` | largest single effective holding weight |
   | `sector_max` | largest sector weight |
   | `top5 / top10` | concentration of the largest holdings |
   | `overlap_max` | largest pairwise scheme overlap |
   | `n_schemes / n_holdings` | counts |

3. **Evaluate each rule** (`webapp/strategy_rules.py::evaluate_rules`):
   `pass = actual <= limit` (or `>=` for min rules). A rule whose metric is unavailable
   (e.g. security-level rules for an allocation-only model) is reported **N/A** and
   excluded from the score. Compliance % = `passed / (passed+failed)`.

4. **Return** `{ compliance, metrics, analysis }`; `analysis` carries the asset/cap/sector
   splits, top holdings, concentration and `debt_analysis` (YTM, maturity, credit quality,
   instrument mix, top debt holdings) plus `cap_schemes` / `debt_schemes` (which funds
   contributed to each cap segment and to the debt sleeve).

## Rule syntax (strategy text box)

Rules are parsed from plain text (`webapp/strategy_rules.py::parse_rules`). Examples:

- `Max 10% single stock.` → `single stock ≤ 10%`
- `Max 20% sector.` → `sector ≤ 20%`
- `Min 30% debt.` / `Max 5% cash.` / `Max 10% gold.` → asset-class bounds
- `Min 20% large cap.` / `Max 25% mid cap.` / `Max 15% small cap.` / `Max 5% microcap.` → cap bounds
- `Max top-5 25%.` `Max overlap 30%.` `Max 5 schemes.` `Min 20 holdings.`

Values may appear before or after the field (`Max 10% single stock` == `single stock max 10%`).
Unrecognised lines are kept as informational notes (not evaluated).

## Coverage — why the pie may not reach 100%

The effective holdings only cover part of the allocated portfolio when:
1. **Some schemes are unresolved** (not in the holdings DB) — their weight is excluded.
2. **A scheme's parsed holdings under-cover NAV** (sum of %NAV < 100) — only that portion
   contributes.

To stay **consistent with the rules** (which use raw portfolio weights), the charts show
**raw weights plus an explicit "Unresolved / Unallocated" slice** that fills to the
allocated total. E.g. if 40.2% of the portfolio is resolved, Equity shows ~31.4% (matching
the rule's actual) and an "Unresolved 59.8%" slice appears. The **coverage %** is shown on
the results card.

### How to increase coverage
- **Resolve missing schemes** (add their holdings to the parsed data, or map the item to a
  matching scheme by ISIN/name).
- **Normalise per-scheme holdings** to 100% so resolved schemes contribute their full weight
  (see `portfolio_analysis`).
- Optionally **estimate unresolved schemes by category** (debt/equity/gold from the fund name)
  as a clearly-labelled approximation.

## Charts

| Chart | Data |
|---|---|
| Top holdings pie | largest holdings + Others + Unresolved (raw) |
| Asset allocation | Equity/Debt/Gold/International/Cash + Unresolved |
| Equity cap split | Large/Mid/Small/Micro/Unclassified/… + Unresolved, plus "Funds holding these" table |
| Debt analysis | YTM, maturity, credit quality, instrument mix, top debt holdings, "Debt via funds" |