# MF Holdings Aggregator — Direction & Data Map

## Goal
Build an app that aggregates the **monthly portfolio holdings of every mutual-fund
scheme of every AMC** in India and matches that holdings data against the **universe
of schemes** (the Combined NAV file) so every scheme has holdings (or an explicit
reason it doesn't — e.g. it is an index/ETF fund whose holdings are the benchmark
composition).

## Reorganized layout (2026-08-20)
Everything relevant now lives under `data/`. App code is under `src/`, registry under
`config/`. Old plans, logs, and scratch scripts are parked in `archive/`.

```
mf_holding/
├── DIRECTION.md             # this file
├── README.md                # overview + quick start
├── main.py                  # CLI entry point (fetch / ingest / report / parse)
├── requirements.txt
├── config/
│   ├── amc_registry.json    # 57 SEBI AMCs + disclosure URLs
│   └── settings.yaml        # paths (all under data/), parser, scheduler
├── docs/                    # all documentation (index: docs/README.md)
│   ├── DATA_SOURCES_RESEARCH.md / SCHEME_DETAILS_STRATEGY.md   # deep specs
│   ├── DEPLOY_RAILWAY.md · DATA_CADENCE.md · ANALYSIS_RESULTS.md
│   ├── MODEL_PORTFOLIOS.md · CLIENT_PORTFOLIOS.md · DATA_HEALTH_PLAN.md
│   ├── APP_REVIEW_ACTIONS.md                    # ops tracker
│   └── plans/                                   # approved build plans (roadmaps)
│       ├── PLAN_SEO_LANDING_PAGES.md
│       ├── PLAN_TRY_APP.md
│       └── PLAN_PERFORMANCE_ANALYTICS.md
├── website/                 # ✅ drop-in SEO pages for the main website (placeholders inside)
├── docs/internal/           # ⛔ git-ignored — internal-only material, never committed
├── data/
│   ├── parsed/                              # ✅ canonical holdings data
│   │   ├── advisorkhoj/                     #   46 AMC JSONs (advisorkhoj pipeline)
│   │   └── amc_websites/                    #   51 AMC dirs (AMC-site pipeline; adds
│   │                                        #     IL&FS, JM, Mahindra Manulife, Taurus,
│   │                                        #     WhiteOak, Zerodha)
│   ├── nifty/                               # ✅ benchmark composition (588 files)
│   │   ├── manifest.json                    #   index → PR/TR series registry
│   │   ├── constituents/                    #   per-index constituent lists (CSV)
│   │   ├── TR/                              #   total-return series
│   │   └── *.csv                            #   price-return series
│   ├── universe/                            # ✅ the scheme universe to match against
│   │   ├── Combined NAV - 14-Aug-2026.csv   #   3,812 fund+plan rows / 2,691 funds
│   │   └── navall.txt                       #   AMFI NAVAll feed (fund-name dictionary)
│   ├── reference/                           # ✅ matching / status artifacts
│   │   ├── equity_isin.db                   #   SQLite: unique equity-segment ISINs + names
│   │   ├── equity_isins.csv                 #   same, as CSV (with confirmed_equity flag)
│   │   ├── amc_download_links.json          #   curated AMC download-page links
│   │   ├── advisorkhoj_catalog.json         #   advisorkhoj per-AMC category links
│   │   ├── advisorkhoj_{download,parse}_report.json
│   │   ├── advisorkhoj_{complete_schemes,partial_schemes}.*
│   │   ├── reconciled_missing.csv           #   133 fund-level missing (checked vs parsed)
│   │   ├── reconciled_active_download.csv   #   2 active (non-index) funds left to fetch
│   │   ├── index_resolved_holdings.json     #   31 equity index funds → 3,846 ISIN holdings
│   │   └── index_unresolved.csv             #   68 index funds not locally resolvable
│   └── raw/                                 # 🗄 raw downloaded documents (kept, large)
│       ├── pdfs/                            #   AMC-site docs (908 files, ~1.2 GB)
│       ├── advisorkhoj/                     #   advisorkhoj docs (348 files, ~520 MB)
│       ├── amc_downloads/                   #   leftover per-AMC batches (513 files)
│       └── manual_ingest/                   #   drop folder for manual ingest
└── archive/                                 # non-essential, kept out of the way
    ├── docs/                                #   old PLAN.md, ADVISORKHOJ_PLAN.md
    ├── scripts/                             #   scratch analysis scripts
    └── logs/                                #   run logs
```

## How holdings are produced (three sources, one priority order)
Holdings for each scheme come from up to three sources, merged in **priority order**
(also see `docs/DATA_SOURCES_RESEARCH.md`):

1. **AMFI — authoritative, preferred.** The official SEBI/AMFI monthly portfolio
   disclosure (aggregated, standardised, `% to NAV` for every holding). Ingested via
   `src/amfi_fetch.py` (mirror `mfdata.in`) → `data/parsed/amfi/`.
2. **Individual AMC websites.** Each AMC's own monthly-portfolio PDF/factsheet
   (`src/amc_adapters/*`, `main.py run`) → `data/parsed/amc_websites/{AMC}/`.
3. **Advisorkhoj** (third-party republisher) → `data/parsed/advisorkhoj/{AMC}.json`.

The webapp data layer (`webapp/db.py`) enforces this priority when picking the
snapshot shown per scheme: `amfi` > `amc_website` > `advisorkhoj` > `index`
(`_SOURCE_PRIORITY`), with the best `%NAV` coverage winning within the same source.
If a scheme's header shows `advisorkhoj`, it means neither AMFI nor an AMC-website
monthly portfolio is on record for it yet.

All sources emit the same scheme-level shape `{scheme_name, plan, date, holdings[]}`
so they merge into one per-AMC holdings set for the app.

## NAV history (daily + backfill)
- **Backfill** (`src/nav_history.py`) builds the full daily history per scheme
  (code → `data/nav_history/<code>.json`) from AMFI's NAV-history report.
- **Daily refresh** (`src/nav_daily.py`) appends the latest NAV points once a day so
  the webapp's NAV charts stay current (see *Automation* below).
- **Gap fill** (`src/fetch_missing_nav.py`) fetches history for codes that have none
  (e.g. ETFs/new funds not in the curated universe CSV).

## Stock agents (prices / corporate actions / NSE reports)
For every `confirmed_equity=1` security (868 stocks) with an NSE symbol:

1. **Identity** — `src/stock_identity.py` builds `data/stocks/identity.json`
   (ISIN → {symbol, name}) from NSE's equity master (`EQUITY_L.csv`) + Nifty
   constituents + a manual override (`data/raw/stock_manual/identity.csv`).
2. **Price history** — `src/stock_price.py` writes
   `data/stock_history/<ISIN>.json` (daily OHLC). Fetch chain: manual CSV
   (`data/raw/stock_manual/<ISIN|SYMBOL>.csv`, the "user sends closing prices"
   path) → **NSE bhavcopy archives** (authoritative daily OHLC, available since
   ~2020; daily files are downloaded by a parallel worker pool and cached under
   `data/stock_bhavcopy/`) → Yahoo Finance (fills pre-2020 history and coverage
   gaps) → Google Finance (latest close only; no public history API).
3. **Corporate actions** — `src/stock_actions.py` → `data/stock_actions/<ISIN>.json`
   (dividends + splits via Yahoo events, cross-checked with NSE announcements).
4. **Recent financial reports** — `src/stock_reports.py` → `data/stock_reports/<ISIN>.json`
   from the NSE `corporate-announcements` API (financial-results category, PDF links).

The webapp reads these live from disk (`webapp/db.py`) — no DB rebuild needed —
and serves `/api/securities/{isin}/price|actions|reports`. The Security Directory
drawer shows a daily-close price chart (reuses the NAV chart), a dividends/splits
table, and a financial-announcements table.

## Model Portfolios · Strategies · Compliance
Advisors create reusable **model portfolios** and **strategies** (plain-text rules),
deploy them to **clients**, and get compliance results vs their constraint limits.

- **Persistence** — user-created data lives in `data/userdata.db` (separate from the
  `data/webapp.db` build cache). Module `webapp/userdata.py` (mirrors `auth.py`).
- **Rules** — `webapp/strategy_rules.py` parses a text box (e.g. *"Max 10% single
  stock. Min 30% debt. Max top-5 25%"*) into structured rules; unparseable lines are
  kept as informational notes. `evaluate_rules()` scores each rule (limit vs actual).
- **API** — `webapp/tools_api.py` (`/api/strategies`, `/api/models`, `/api/clients`,
  `/api/client-portfolios`, `/api/analyze`). All user-scoped + auth-gated.
- **UI** — "Model Portfolios" screen: Strategies / Models / Clients / Client
  Portfolios (deploy) / Analysis. Models reuse the shared portfolio builder
  (`pfMount`). Analysis shows a per-rule compliance table (limit vs actual vs
  pass/breach), overall compliance %, and the top-holdings donut.
- **Integration** — Portfolio Tools has **Save as model**; Proposal Generator has
  **Load client portfolio** (loads the client's lines into the builder and generates
  an editable proposal).

## Automation (scheduler)

`main.py schedule-start` runs all jobs on one `AsyncIOScheduler` (`src/scheduler.py`):

1. **Monthly holdings fetch** — days 1–5 of each month (`scheduler.day_of_month`,
   `retry_days`), runs the full fetch+parse pipeline.
2. **Daily NAV refresh** — every day at `scheduler.nav_refresh.hour:minute`, appends
   the latest AMFI NAVs to `data/nav_history/` (see `config/settings.yaml`).
3. **Daily stock refresh** — every day at `scheduler.stock_refresh.hour:minute`,
   refreshes identity + closing prices + corporate actions + NSE reports
   (`src/stock_refresh.py`).

Manual triggers: `python main.py nav-daily [--days N]`, `python main.py stock-refresh`,
`python main.py stock-price --symbols ADANIENT`, etc. (`python main.py --help`).

## Working way (ops)
- **Track work as TODOs** — each task and attempt is tracked; update status as work
  progresses (pending → in_progress → completed/cancelled).
- **Restart the webapp safely** with `scripts/restart_webapp.ps1` — it stops any
  running `python -m webapp`, starts a fresh instance in the background, and
  health-checks `http://127.0.0.1:8000/api/health` until it responds.
- After data-layer changes (`webapp/db.py`), delete `data/webapp.db` to force a
  rebuild on next boot, then restart via the script above.

## Matching logic (how "missing" is decided)
- **Direct ≡ Regular**: both plans hold the same portfolio, so schemes are compared at
  **fund level** (dedupe Direct/Regular).
- **Index / ETF funds** (`idx_based` / `is_index` / `is_etf`): holdings equal the
  benchmark, which we already have in `data/nifty/` — no AMC download required.
- Everything else must have a holdings document; those with none are the
  **download backlog** (`data/reference/missing_active_download.csv`).

### Current status (14-Aug-2026 universe · refreshed 23-Aug-2026)
- Universe: **2,691 funds / 3,812 fund+plan rows** across 51 AMCs (registry has 57).
- **Naive missing fund-level schemes: 332** — but the original analysis only looked at
  `all_schemes.csv` (deleted), which **never merged the advisorkhoj parsed schemes**.
  `src/reconcile_missing.py` now regenerates the missing list **directly from the Combined
  NAV universe** against the actual parsed JSONs → **~130 true missing**, of which
  **2 active (non-index)** remain in the download backlog
  (`data/reference/reconciled_active_download.csv`: HDFC Credit Risk Debt, UTI Credit Risk).
- **Index / ETF bucket — `src/index_resolver.py` (23-Aug run):**
  - **291 resolved** → mapped to Nifty constituent lists → **23,900 ISIN holdings**
    in `data/reference/index_resolved_holdings.json` (bank-index keywords added:
    Private Bank / PSU Bank / FinServices-ex-Bank).
  - **499 unresolved locally** (`data/reference/index_unresolved.csv`):
    **116 debt-index** (index stats only, no ISIN securities), **93 BSE**,
    **77 commodity** (Gold/Silver), **7 Nasdaq**, **5 MSCI**, rest plan-variants.
- **Covered: 2,050 / 2,096 fund-level schemes (97.8%)** — every active fund has holdings.
- **`data/reference/discovery_needed.csv` (32 genuinely-unfilled):**
  - ~~14 equity-Nifty index/ETF~~ → **RESOLVED 23-Aug** via index_resolver (Tata Private
    Bank needed a missing INDEX_MAP entry; the rest were already resolving)
  - **9 debt-index** → need NSE/BSE index **weight** data (we only hold index stats)
  - **8 no-disclosure** (HSBC Climate, AlphaGrep ×3, NJ Momentum/Value, Abakkus LMC, Canara BFS)
  - **7 plan/segregated variants** (Kotak Gilt/Infra SP, Franklin G-Sec/Short Term, ABSL 50s-Plus-Debt) — verify holdings source
  - **4 commodity** (Gold/Silver), **3 BSE**, **1 MSCI/Nasdaq** (need external index data)
- **ISIN completeness: equity-stock holdings missing ISIN reduced 32,549 → 2,019 (94%).**
  Done via (1) monthly-XLS rebuilds of ABSL/DSP/Motilal/Bandhan/UTI/Kotak/Axis/Franklin/
  Edelweiss/HDFC/HSBC into `data/parsed/amc_websites/` (most now ~95-100% ISIN coverage)
  and (2) ISIN backfill from the Nifty reference (`src/nifty_isin_lookup.py`: exact +
  fuzzy + distinctive-token match). Remaining ~2k are parse-noise (return-table headers,
  truncated names) + unlisted/foreign holdings.

## Direction / next steps
1. **Close the download backlog**: work AMC-by-AMC from `missing_active_download.csv`,
   using the curated links in `data/reference/amc_download_links.json`. The user
   supplies each AMC's monthly-disclosure / factsheet page URL; record it in that JSON
   and fetch+parse into `data/parsed/amc_websites/`.
2. **Unify parsed data**: build a merge step that combines `advisorkhoj/*.json` +
   `amc_websites/*` into a single normalized holdings store keyed by AMC→scheme, and
   regenerates `coverage_*` / `missing_*` from the universe.
3. **Resolve the 4 registered-but-URL-less AMCs** (Carnelian, Lakshya, Monarch,
   Nuvama) + ASK (no disclosures yet) + AlphaGrep (obfuscated URL).
4. **Reference index holdings**: `src/index_resolver.py` already maps the 31 equity
   Nifty index/ETF funds to `data/nifty/constituents/` (3,846 ISIN holdings). Next:
   the 68 unresolved need outside sources — NSE index weights for **debt** indices,
   and BSE / MSCI / Nasdaq / commodity feeds.
5. **Webapp** (future): a dashboard that for any scheme in `universe/` shows
   holdings, source (AMC PDF / advisorkhoj / index), date, and coverage status, and
   flags schemes still missing data.

## Housekeeping
- `data/raw/*` is large but intentionally kept (source documents, re-parseable).
- `archive/` holds superseded plans/logs/scripts — can be emptied once no longer needed.
- Regenerate `__pycache__` automatically; none are kept in the repo.
