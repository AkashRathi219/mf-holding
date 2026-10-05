# NSE / Stock Data Sources

Equity pipeline: identity → prices → corporate actions → announcements →
financial statements. All NSE JSON calls use a shared cookie-warmed session;
all HTTP is GET (POST only for AI/captcha tiers).

---

## 1. Shared transport — `src/stock_common.py`

- `NSE_HOME = "https://www.nseindia.com/"`.
- `nse_session()`: process-cached `urllib` opener + `CookieJar`; warms cookies
  with one best-effort GET to the home page (Chrome UA). NSE `api/*` calls use
  `retries=1` deliberately so Akamai blocks fail fast instead of hanging a
  multi-hour refresh.
- `http_get()`: generic GET with browser UA, gzip, retries default 3,
  backoff 2/4/8 s. TLS verification disabled globally.
- Paths: `data/stocks/identity.json`, `data/stock_history/`,
  `data/stock_actions/`, `data/stock_reports/`, `data/raw/stock_manual/`.

---

## 2. Identity — `src/stock_identity.py`

| Item | Value |
|---|---|
| Source | `GET https://archives.nseindia.com/content/equities/EQUITY_L.csv` |
| Fields | `SYMBOL`, name, `ISIN NUMBER` |
| Priority chain | `manual` (`data/raw/stock_manual/identity.csv`) > `nse_equity_list` (EQUITY_L) > `nifty_constituents` (`data/nifty/constituents/*.csv`) > `name_only` (no symbol → price/actions/reports skipped) |
| Output | `data/stocks/identity.json` = `{ISIN: {symbol, name, source}}` |
| Cadence | Loaded (not refreshed) at step 1/4 of every daily `stock_refresh`; forced with `python main.py stock-identity` or `python -m src.stock_identity --force` |

---

## 3. Daily prices — `src/stock_price.py`

### 3.1 Endpoints

| Tier | URL | Notes |
|---|---|---|
| Primary bhavcopy | `https://archives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv` | Rejected unless body starts `SYMBOL`; cached `data/stock_bhavcopy/<YYYYMMDD>.csv` |
| Fallback bhavcopy | `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | UDiFF format (NSE deprecated legacy w.e.f. Jul-2024); inner CSV must start `TradDt` |
| Pre-2020 history | `https://www.nseindia.com/api/historical/cm/equity?symbol=<SYM>&series="EQ"&from=DD-MM-YYYY&to=DD-MM-YYYY` | `curl_cffi` `impersonate="chrome124"`, NSE-home warm-up, 1-year chunks, 1.5 s pacing; failures return `[]` |
| Google (top-up) | `https://www.google.com/finance/quote/<SYM>:NSE` | Regex `data-last-price` (Indian commas handled); latest close only |
| Yahoo (range fill) | crumb `https://query1.finance.yahoo.com/v1/test/getcrumb`, chart `https://query1.finance.yahoo.com/v8/finance/chart/<SYM>.NS?period1=&period2=&interval=1d&crumb=` | Session warmed at `https://fc.yahoo.com`; returns closes only |
| Manual CSV | `data/raw/stock_manual/<ISIN>.csv` / `<SYMBOL>.csv` | `date,close` or `date,open,high,low,close,volume`; authoritative |

### 3.2 Fallback chain per symbol (`refresh_stock`)

```
manual CSV  →  NSE bhavcopy (CSV → UDiFF ZIP, bhavcopy wins per date)
            →  Google Finance (only daily=True; latest close)
            →  Yahoo chart (range fill / pre-2020 depth)
            →  keep existing / no_data
```

Note: `yahoo_allowed()` (`STOCK_ALLOW_YAHOO`) is dead code in this module —
Yahoo is attempted unconditionally; the flag only gates
`stock_actions.backfill_empty_from_yahoo`.

### 3.3 Modes & outputs

| Mode | Command | Scope |
|---|---|---|
| Daily incremental | `python -m src.stock_price --daily` (or via `stock_refresh`) | bhavcopy window today−15 d |
| Full backfill | `python -m src.stock_price` | bhavcopy 2020-01-01 → today, weekday files |
| Download-only | `--download-only` | bhavcopy top-up only |
| Pre-2020 dump | `--dump-nse-history` | NSE historical API 1994→2019 → `data/raw/nse_historical/<SYMBOL>.json` (resumable `_status.json`) |
| Local rebuild | `--rebackfill-nse [--symbols] [--force]` | No network: local dumps + bhavcopy, bhavcopy wins overlap, checkpoint `data/stock_bhavcopy/nse_backfill_status.json` |
| CLI wrappers | `python main.py stock-price [--symbols --daily --limit]` | — |

Output `data/stock_history/<ISIN>.json`:

```json
{"isin": "...", "symbol": "...", "name": "...", "currency": "INR",
 "source": "NSE bhavcopy", "fetched_at": "...",
 "history": [{"date": "...", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}]}
```

Caches: `data/stock_bhavcopy/*.csv`. No DB writes. Downloads parallelised
(ThreadPoolExecutor, default 10 workers); legacy split-adjustment watermark
`splits_applied_through` exists for bhavcopy-era points.

---

## 4. Corporate actions — `src/stock_actions.py`

| Source | URL | Role |
|---|---|---|
| Yahoo events | `https://query1.finance.yahoo.com/v8/finance/chart/<SYM>.NS?range=max&interval=1d&events=div%2Csplit` | Daily primary (dividends + splits) |
| NSE announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>` | Supplementary keyword hits (`dividend/bonus/split/rights`) |
| NSE structured actions | `https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol=<SYM>` | Backfill primary; latest ~20 rows only, no date filters |

Fallback chains:

- **Daily** (`refresh_actions`): Yahoo events → NSE announcements (only when
  Yahoo succeeded) → keep previous on empty (status `kept_previous`).
- **Backfill** (`fill_actions_from_dumps`): NSE structured dump → announcement
  keyword hits for empty windows → Yahoo events only if NSE yielded zero
  (flag-gated `STOCK_ALLOW_YAHOO=1`) → near-duplicate split suppression (60-day
  window) → refuses to shrink curated history (`aborted_shrink`).

Output `data/stock_actions/<ISIN>.json` = dividends `{date, amount}`,
splits `{date, ratio}`, announcements, sources. Raw dumps:
`data/raw/nse_actions/`, `data/raw/yahoo_actions/` (each with `_status.json`).
Commands: `python -m src.stock_actions --dump-nse-actions /
--backfill-empty-actions / --backfill-empty-yahoo / --fill-from-dumps`;
`python main.py stock-actions`.

---

## 5. Result announcements — `src/stock_reports.py`

- Source: NSE `corporate-announcements` (single first page, latest 20).
- Filter `_FIN_CATS`: financial/quarterly/annual results, profit.
- Output `data/stock_reports/<ISIN>.json`
  (`{date, headline, category: "financial_results", url (PDF), size}`).
- On empty/failed response keeps previous announcements (never wipes).
- Daily 21:00 IST inside `stock_refresh`; CLI `python main.py stock-reports`.

---

## 6. Financial statements — `src/financial_statements.py`

| Stage | Source / tool | Notes |
|---|---|---|
| 1. Filing list | `GET https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>&page=<N>` | 3 attempts/page (2 s, 8 s backoff), 0.4 s between pages; `process_stock` uses 2 pages, bulk scripts 30 |
| 2. PDF download | `attchmntFile` URL via nse_session | Must start `%PDF-`; cached `data/raw/financial_results/<SYMBOL>/` |
| 3. Deterministic parse | `pdfplumber` word-position parser, 90 s child-process guard | SEBI Reg-33 tables → canonical schema (units normalised to ₹ crore) |
| 4. AI vision tier | `POST https://openrouter.ai/api/v1/chat/completions` (`STMT_BACKEND=openrouter`, default model `google/gemini-2.5-flash`) or local `opencode run -m <model>` | **Disabled in the scheduled path** (`process_stock`); reachable via `scripts/pull_annual_results.py`; cache `_ai_cache/<sha>-p<page>-p3.json` |

Output `data/stock_financials/<ISIN>.json`: `consolidated`/`standalone`
`{quarters[], annual[], ttm}`, `sources[{url, date, sha256, tier}]`,
`validation{issues, confidence}`. Derived quarters/TTM computed from cumulative
columns.

Cadence: weekly Sunday 06:30 IST (`statements_refresh`, stale-first,
`STALE_DAYS=35`, webapp limit 12, most-held equities first via `webapp.db`).
Commands: `python -m src.financial_statements [--symbols --limit]`,
`python main.py statements-refresh` (via CLI wiring).

### Bulk rebuild scripts

| Script | Purpose | Notes |
|---|---|---|
| `scripts/download_results.py` | Network-only phase: announcement snapshots + PDFs | 5 workers, 3 passes, 90 s pause; universe = NIFTY 50 |
| `scripts/pull_annual_results.py` | Rebuild statements from feed/PDFs (`--from-local` zero-network) | URL ranking by filename, audit classification, AI when deterministic parse empty and period ≥ FY2022; status `data/raw/financial_results/annual_fy25_26_status.json` |
| `scripts/run_pull_waves.py` | Multi-agent wave coordinator (5 × 10 symbols, 45 s stagger) | Parallel statement rebuilds |
| `scripts/run_pull.ps1`, `scripts/close_stmt_gap.ps1` | Streaming logs / deep gap close (pages 100 → re-parse → coverage) | — |

---

## 7. External results-comparision CSV — `src/ingest_output_financials.py`

- **No network.** Ingests `output/<SYMBOL>/nse_financials_latest5q.csv`, a file
  produced by a worker that is **not in this repository** (NSE
  results-comparision dump: revenue, PBT, PAT, EPS, ...).
- Units: NSE reports ₹ lakh → converted to crore (flows ÷100; EPS/face value
  unscaled).
- Merge rule: already-parsed PDF records win; the CSV fills missing
  `(kind, period_end)` records only; TTM recomputed.
- CLI: `python -m src.ingest_output_financials [--symbol SYM] [--dry-run]`.

---

## 8. Cadence & health

| Job | Schedule (IST) | Pipeline |
|---|---|---|
| `daily_stock_refresh` | 21:00 daily | identity → prices → actions → reports (`src/stock_refresh.py`) |
| `statements_refresh` | Sunday 06:30 | stale-first statements, limit 12 (webapp) |
| Manual | — | backfills, pre-2020 dump, annual-results waves, CSV ingest |

Telemetry: `track("stock_refresh")`, `track("statements_refresh")` in
`data/logs/refresh_log.jsonl` / `refresh_state.json`. Health expectations:
stock 48 h, statements not in `CADENCE_HOURS`; `MAX_AGE_DAYS=10` for price files
(`webapp/data_health.py:38-41`).

Webapp consumers: `data/stocks/identity.json`, `data/stock_history/<ISIN>.json`,
`data/stock_actions/<ISIN>.json`, `data/stock_financials/<ISIN>.json` (R2-synced
via deploy staging).
