# Nifty / Index Data Sources

Index constituents, benchmark weights, and total-return series used for
index/ETF funds, stock identity canonicalisation, and benchmark comparisons.

---

## 1. Constituent corpora (local files, no fetcher in-repo)

| Directory | Content |
|---|---|
| `data/nifty/constituents/` | ~200 CSVs per index (Company, Industry, Symbol, Series, ISIN Code); `manifest.json` maps niftyindices.com index URLs → downloaded files with row counts; `mapping.json` for index statistics |
| `data/nifty/debt_constituents/` | Debt/hybrid index CSVs (ISIN + Weight %), index-statistics files, Zerodha fund-holdings workbooks |
| `data/nifty/TR/` | Total-return series per index, header `Date,TotalReturnsIndex,NTR_Value` (e.g. `NIFTY_50.csv`); consumed by `webapp/db._load_tr_index` with lazy R2 ensure |
| `data/nifty/weights.json` | Benchmark weights, produced by §3 |

These files are corpus-managed (downloaded once / periodically outside the
scheduler) and shipped to R2 by `deploy/prepare_data.py`.

---

## 2. Consumers of the corpora

| Module | Role | Output |
|---|---|---|
| `src/index_resolver.py` | Maps index/ETF schemes to constituent CSVs via keyword `INDEX_MAP` (most-specific-first, e.g. `nifty 500 multicap 50:25:25` before `nifty 50`); debt indices via `DEBT_INDEX_MAP`. Equity CSVs use `Company Name`/`ISIN Code`, debt use `ISIN`/`SECURITY_NAME`/`Weight %` | `data/reference/index_resolved_holdings.json` (fund→holdings, source `index`/`index-debt`), `data/reference/index_unresolved.csv` |
| `src/nifty_isin_lookup.py` | Name→ISIN lookup in the corpora (exact normalized, then token overlap with distinctive-token rule) | library (`lookup_isin`) |
| `src/clean_equity_isins.py` | Rebuilds `data/reference/equity_isins.csv` + `.db` (canonical NSE name, sector, `confirmed_equity=1`, cap bucket by index membership: NIFTY_100→large, Midcap_150→mid, Smallcap_250→small, Microcap_250→microcap, SME_EMERGE→sme) | `data/reference/equity_isin.db` |
| `webapp/db.py` | Applies `weights.json` to index schemes by `index_name` before market-value/equal-weight fallbacks; lazy R2 ensure for TR files | webapp holdings/benchmark APIs |

Priority in the webapp: source `amfi > amc_website > advisorkhoj > index`
(`webapp/db.py:1153`).

---

## 3. Benchmark weights — `webapp/nifty_weights.py`

| Item | Value |
|---|---|
| Source | `GET https://www.niftyindices.com/Factsheet/ind_<code>.pdf` (Firefox UA, timeout 45, follows redirects, requires `%PDF`) |
| Parsing | `pdfplumber` text; matches constituents (first two name tokens) to the first decimal after the name; unmapped tail constituents get equal weight so each index sums to ~100% |
| Factsheet codes | NIFTY_50, NIFTY_100, NIFTY_Next_50, NIFTY_Smallcap_100, Nifty_Smallcap_250, Nifty_Midcap_150, Nifty_Bank, Nifty_IT (7 indices + Smallcap 250; 474 securities) |
| No reachable PDF | Strategy/thematic indices (LargeMidcap_250, Nifty200_Momentum_30, Nifty200_Alpha_30, Nifty_Capital_Markets, Nifty_Oil_and_Gas) → equal weight or `--file` ingest |
| File fallback | `--file weights.csv|xlsx|json` with `index/isin/weight` columns (only `INE*` ISINs) |
| Output | `data/nifty/weights.json` = `{ index_name: { ISIN: weight_pct } }` |
| Command | `python -m webapp.nifty_weights [--file ...] [--index ...]` |

Alternate documented sources (bot-blocked from dev env): niftyindices.com
monthly "Indices Market Capitalisation & Weightage" ZIP report; NSE
`equity-stockIndices` API.

---

## 4. Cadence

| Data | Cadence | Trigger |
|---|---|---|
| Constituent CSVs / TR series | Periodic corpus refresh (outside scheduler) | Manual download + R2 staging |
| `weights.json` | Per index-review cycle | Manual `python -m webapp.nifty_weights` |
| `index_resolved_holdings.json` | When corpora update | Manual `python -m src.index_resolver` |
| `equity_isins.db` | When corpora update | Manual `python -m src.clean_equity_isins` |

No scheduler jobs fetch niftyindices.com or NSE index pages today.
