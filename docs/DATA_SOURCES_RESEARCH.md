# Holdings & %NAV Data Sources — Research Report

**Goal:** obtain, for every scheme of every AMC, the **holdings and their percentage
breakup (%NAV)** so the Portfolio Overlap tool produces correct, meaningful results.
For index / ETF funds, use the **Nifty benchmark composition weights**.

This report audits every available source, states what it provides, and records the
pipeline changes implemented (and what remains).

---

## 1. The three core sources

### 1.1 AMFI — Monthly Portfolio Disclosure (authoritative, preferred)
- **URLs:**
  - `https://www.amfiindia.com/online-center/portfolio-disclosure`
  - `https://www.amfiindia.com/otherdata/scheme-wise-disclosure` (SEBI circular 25-Aug-2022)
- **What it provides:** AMFI aggregates the **complete monthly portfolio of every
  scheme of every AMC**, in a standardised machine-readable (Excel/CSV) format, within
  10 working days of month-end. Standard columns (SEBI CIR/IMD/DF/21/2012 +
  2020-43 master circular):
  Company/Issuer · ISIN · Industry/Sector · Instrument type (Equity/NCD/G-Sec/T-Bill/
  CP/CD/REIT/InvIT) · Rating (debt) · Quantity/Face value · **Market value** ·
  **% to NAV** · Yield (debt).
- **Why it is the preferred source:** it has **% to NAV for every holding** — exactly
  the primary outcome — across all schemes/AMCs in one place.
- **Access:** the page is a JS SPA; data is fetched from AMFI's internal API. A public
  mirror with the same fields (free, no auth) is available at **mfdata.in**
  (`GET /api/v1/families/{id}/holdings` returns `equity_holdings[...].weight_pct`).

### 1.2 Advisorkhoj
- **Data:** `data/parsed/advisorkhoj/*.json` — 46 AMCs, scheme-level with real fund
  names, `name`, `quantity`, `value` (₹ lakh), `isin`, `industry`.
- **%NAV:** **often absent** (e.g. PPFAS has none) — 82% of advisorkhoj holdings carry
  `pct_nav`; the rest have `value` from which % can be **computed**.
- **Use:** good for **scheme-name identity** and holdings ISINs; weaker for weights.

### 1.3 Individual AMC websites (monthly portfolio)
- **Data:** `data/parsed/amc_websites/**` — 51 AMCs. Best-quality weights:
  `percent_nav` (as a **fraction 0–1**) + `section` (Equity/Debt) + `market_value`.
- **Caveats found in audit:**
  - scheme name is often a **sheet code** (PPFAS: `PPFCF`, `PPLF`, `PPCHF`…) →
    discarded as junk today (see §4, TODO);
  - some files are **grouped factsheets** (partial top-holdings, mangled names) —
    **now excluded** from holdings loading (§5).
- **Use:** best-quality **weights** when a full monthly-portfolio file exists.

### 1.4 Index / ETF benchmark composition (for index funds)
- **Requirement:** for index/ETF funds, weights should follow the **Nifty benchmark**
  the scheme tracks.
- **Local data:** `data/nifty/constituents/*.csv` list constituents (Company, Industry,
  Symbol, Series, ISIN) but **no weights**. `data/nifty/debt_constituents/*` carry
  index stats (yield/maturity) but no per-security weights.
- **External weight sources (available):**
  - **niftyindices.com** monthly "Indices Market Capitalisation & Weightage" report
    (zip) and **`get_index_constituents`** API → per-stock `name`, `weight`, `sector`,
    `date` (ISIN not returned by the public API — join to constituents by symbol/name).
  - **NSE** index-tracker pages; open-source scrapers (e.g. `nifiio`) return
    `Symbol → Weightage` + ISIN.
- **Implemented:** `data/nifty/weights.json` ingestion scaffold
  `{ index_name: { isin: weight_pct } }` (see §5). Until real weights are ingested,
  index funds fall back to **equal weight** so overlap still computes.

---

## 2. %NAV availability (before this work)

| Source | Holdings | with %NAV | % |
|---|---|---|---|
| advisorkhoj | 88,524 | 73,095 | 82% |
| amc_website | 54,501 | 51,170 | 94% |
| index-resolved | 13,014 | 0 | 0% |

- **420 / 2,717** schemes had **no %NAV at all** (overlap returned 0 for them).
- **16,539** holdings had `market_value` but no `%NAV` (weight computable).

---

## 3. Implemented pipeline changes (`webapp/db.py`)

1. **Source selection prefers the authoritative source, then weights.** Among the
   same fund's source snapshots, choose by **source priority** first
   (`amfi` > `amc_website` > `advisorkhoj` > `index` — `_SOURCE_PRIORITY` in
   `webapp/db.py`), then the snapshot with the **highest fraction of rows carrying
   `percent_nav`** (then most weighted rows, then ISIN coverage, then count).
2. **Excluded grouped factsheets** from holdings loading (partial top-holdings with
   mangled names) — they are used only for returns/benchmark extraction.
3. **%NAV fallback from market value.** Any holding with `market_value` but no `%NAV`
   gets `pct = market_value / Σ market_value × 100` (scale-invariant).
4. **Equal-weight fallback.** Schemes still without weights (index/ETF) get
   `100/n` per holding, so **every** scheme has a numeric breakup.
5. **Nifty benchmark weights scaffold.** `data/nifty/weights.json` →
   `{ index_name: { isin: weight_pct } }` is applied to index schemes by `index_name`
   before the mv/equal-weight fallbacks. Source: niftyindices.com monthly weightage.

### Result
- **Weightless schemes: 420 → 0.** Every scheme now exposes a %NAV per holding.
- **Parag Parikh Flexi Cap Fund:** advisorkhoj (129 holdings) → computed weights,
  Σ = 100%; top = HDFC Bank 7.17%, Power Grid 5.68%, ITC 5.46%, ICICI 5.28%.
- **Overlap verified:** Parag Parikh Flexi Cap vs HDFC Flexi Cap = **31%** (symmetric);
  concentration top = ICICI 14.5%, HDFC Bank 13.3%, Axis 8.7%. Two Nifty-50 ETFs ≈ 66%
  (equal-weight fallback until real benchmark weights are ingested).

---

## 4. Remaining work (recommended next steps)

1. **Code → fund resolution** so weighted AMC-site portfolios with sheet codes
   (PPFAS `PPFCF` etc.) map to their real scheme. Build the map from the same AMC's
   factsheet or advisorkhoj (code appears alongside the full name), then don't discard
   those files. This upgrades PPFAS from *computed* to *sourced* weights.
2. **Bulk AMFI ingestion.** Add an AMFI (or mfdata.in) connector to pull the
   aggregated monthly disclosure → the single most complete %NAV source across all
   schemes/AMCs; merge it as the highest-priority weighted source.
3. **Ingest real Nifty weights.** Fetch niftyindices.com monthly weightage →
   write `data/nifty/weights.json` so index/ETF funds show **actual benchmark
   composition** instead of equal weight.
4. **Tag weight origin** per scheme (sourced / computed-from-mv / estimated-equal) so
   the UI can label it and analysts can trust the number.
5. **Normalise `section`** (equity/debt/cash) consistently (already specced in
   `SCHEME_DETAILS_STRATEGY.md`) so the equity-vs-debt breakup is reliable.

---

## 5. Quick reference — where weights come from, in priority order

1. Source `percent_nav` (AMFI / amc_website, normalized fraction↔percent)
2. Nifty benchmark weights (`data/nifty/weights.json`) for index/ETF
3. Computed from `market_value` (`mv / Σmv`)
4. Equal weight (`100/n`) — always yields a usable overlap

---

## 6. Nifty benchmark-weight ingestion (implemented)

`python -m webapp.nifty_weights` builds `data/nifty/weights.json`
(`{ index: { isin: weight_pct } }`), which the data layer applies to index/ETF
funds by their `index_name` (verified: Nifty 50 funds now show HDFC Bank 10.27%,
ICICI 9.22%, Reliance 7.92%…).

Sources:
- **Full-weight file** (accurate): drop a CSV/XLSX/JSON from the niftyindices.com
  "Market Capitalisation & Weightage" report or the NSE `equity-stockIndices` API:
  `python -m webapp.nifty_weights --file weights.csv`.
- **Live factsheet PDFs** (top constituents only, tail equal-weighted): fetched from
  `niftyindices.com/Factsheet/ind_<code>.pdf`. Resolved factsheet codes now cover:
  **NIFTY_50, NIFTY_100, NIFTY_Next_50, NIFTY_Smallcap_100, Nifty_Bank, Nifty_IT,
  Nifty_Midcap_150** (7 indices, 474 securities, each summing to 100%). Index ETFs
  verified: Nifty IT (Infosys 29.2%, TCS 20.3%), Nifty Bank (HDFC 18.2%, ICICI 14.9%),
  Nifty Next 50 (TVS 4.0%, Tata Motors 3.6%). The strategy/thematic indices
  (`NIFTY_LargeMidcap_250`, `Nifty200_Momentum_30`, `Nifty200_Alpha_30`,
  `Nifty_Capital_Markets`, `Nifty_Oil_and_Gas_Index`) have no reachable factsheet PDF
  in the `ind_*.pdf` pattern — they fall back to equal weight (or a `--file` ingest).
  (`blob.niftyindices.com` and `nseindia.com` are bot-blocked/DNS-down from here.)

Indices without ingested weights continue to use equal weight, so overlap still
computes; adding full weights only makes the numbers more precise.
