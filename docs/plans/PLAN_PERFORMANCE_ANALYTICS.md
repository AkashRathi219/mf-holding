# Plan — Performance & Risk Analytics Engine (staging)

Status: PLANNED (queued) · Created: 22-Aug-2026
Related: `PLAN_TRY_APP.md` (retail extension), `../internal/COMPLIANCE_CHECKLIST.md` (guardrails)

## Question addressed

Can we add rolling returns, past performance of schemes, Sharpe / Sortino /
Information ratio — for single schemes, multi-scheme comparison, and client
portfolios? **Yes.** The data foundation already exists; gaps are compute +
two data decisions.

## Data availability

| Metric group | Data we have | Gap |
|---|---|---|
| Trailing / CAGR returns (1Y/3Y/5Y/since-inception) | `data/nav_history/<code>.json` (daily, auto-refreshed) | none |
| Rolling returns | same NAV series | compute engine only |
| Sharpe / Sortino | NAV returns + risk-free rate | need an rf source |
| Beta / Alpha / Treynor / Information ratio / Tracking error | NAV series + `data/nifty/TR/` benchmark series | category→benchmark auto-mapping |
| Max drawdown / volatility | NAV series | none |
| Multi-scheme comparison | overlap API pattern (2–12 schemes) already exists | analytics endpoint + UI |
| Client-portfolio level | portfolio builder + `webapp/market_value.py` weights | reconstruct portfolio NAV series from weighted scheme NAVs |

### Open decisions
1. **Risk-free source**: 91-day T-bill index (preferred) vs fixed documented
   assumption. Must be stated on the methodology page either way.
2. **Benchmark mapping**: scheme category → appropriate Nifty TR index;
   index/ETF funds resolvable via `data/reference/index_resolved_holdings.json`.

## Staging (each phase ships standalone value)

1. **Scheme metrics engine** — `/api/schemes/{id}/analytics`: CAGR,
   rolling-return distribution (% positive periods), volatility, Sharpe,
   Sortino, max drawdown, beta/alpha/IR vs mapped benchmark.
   Shown in the existing scheme drawer.
2. **Compare view** — pick 2–12 schemes → metric table + growth-of-₹100 chart +
   rolling-return chart side-by-side. Reuses overlap-selection UX.
3. **Portfolio-level analytics** — same suite over reconstructed client-portfolio
   NAV series inside Model Portfolios / Analysis screen.
4. **Proposal integration** — performance tables in white-label proposals with
   locked disclaimers + timestamped methodology-version stamp (audit trail).
5. **Marketing sync** — landing-page features grid + methodology page updated;
   strengthens value proposition to "institutional-grade diagnostics".

Ordering rationale: scheme metrics first (cheapest, biggest wow),
comparison second (differentiator vs spreadsheets), portfolio third (stickiness),
proposals fourth (workflow lock-in).

## Compliance guardrails (hard requirements)

Displaying *factual scheme performance* (from AMFI NAVs) is the Value
Research/Morningstar model — a data platform doing this is not "making
performance claims" under SEBI's association rules, which target claims about
one's own recommendations/advisory track record. Stay on the safe side:

- Every figure carries **as-of date + computation window**
  ("1Y rolling returns, daily steps, since 01-Jan-2016")
- Neutral language only: **"percentile in category"** — never
  "top pick / best fund / recommended"
- Standard disclaimer everywhere: *"Past performance is not indicative of future returns"*
- Generated proposals embed **AMFI-standardized formats** (₹1L-growth,
  1Y absolute + 3/5/10Y CAGR vs benchmark) so adviser-customers automatically
  satisfy the Advertisement Code
- No forward-looking statements, no buy/sell signals, no target prices
- Methodology page documents every formula (also satisfies customers'
  Regulation 16C "glass-box" due diligence)
- Client-portfolio past performance = diagnostic of *their own* portfolio — fine;
  never frame as "adviser track record"

## Verification checklist (per phase)

- [ ] Metrics unit-tested against hand-computed values on known series
- [ ] As-of dates and windows rendered next to every figure
- [ ] Copy review against guardrails above
- [ ] Proposal output includes locked disclaimer block
