# Data Sources — Master Map

Every external source the pipeline fetches or scrapes, how it is accessed, how
often, and where the result lands on disk. Detail per domain lives in the
sibling files:

| File | Domain |
|---|---|
| [amfi.md](amfi.md) | AMFI NAV portal, AMFI JSON APIs, TER, portal directory, SIF NAV, legacy mirrors |
| [amc-websites.md](amc-websites.md) | 57 AMC websites: adapters, scraping techniques, PDF/Excel/HTML parsing, metadata miners |
| [nse-stocks.md](nse-stocks.md) | NSE equity master, bhavcopy prices, corporate actions, announcements, results PDFs, Google/Yahoo fallbacks |
| [nav-history.md](nav-history.md) | NAV history fetch/backfill/repair/freshness, R2 mirror, on-disk schema |
| [nifty-indices.md](nifty-indices.md) | Nifty constituent CSVs, benchmark weights, total-return series |
| [bonds.md](bonds.md) | NSE corporate-bond master, WDM list, CBM trades, live debt-market API |
| [cadence-matrix.md](cadence-matrix.md) | All scheduler jobs, CLI commands, freshness expectations |

Data policy in force: **AMFI / AMC / NSE (niftyindices for index weights) only**
— third-party mirrors are retired or kept as off-schedule legacy utilities.

---

## 1. Source registry (one row per upstream)

| # | Source | What we pull | How it is accessed | Frequency | Code | Output |
|---|---|---|---|---|---|---|
| 1 | `portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx` | Daily NAV per scheme since 2006 | stdlib `urllib` GET, `tp=1&frmdt&todt`, 90-day chunks, gzip, TLS verify off | Daily 23:30 + 08:30 IST; backfills on demand | `src/nav_history.py:56`, `src/nav_daily.py`, `src/nav_freshness.py` | `data/nav_history/<code>.json`, staging `data/nav_history/.staging/nav.db` |
| 2 | `www.amfiindia.com/api/*` (17 endpoints) | Tracking error/difference, scheme-wise disclosure, risk params, AUM, NFO, dividends, scheme details | `httpx` GET (JSON), `Referer` per endpoint, retries 3 | Monthly, days 8–12, 07:45 IST | `src/amfi_otherdata.py` | `data/reference/amfi_*.json`, raw `data/raw/amfi_otherdata/**` |
| 3 | `www.amfiindia.com/api/populate-te-rdata-revised` | TER (Regular/Direct Total TER %) | `httpx` GET XLSX export | Manual CLI, monthly (`python main.py ter`) | `src/amfi_ter.py` | `data/raw/ter/TER_<MM-YYYY>.xlsx`, `data/reference/ter_<month>_*.csv`, `data/reference/ter_by_isin.json` |
| 4 | `www.amfiindia.com/online-center/portfolio-disclosure` + `/api/sif-latest-nav` | AMFI disclosure-member directory (mf_id, URLs); SIF latest NAV | `httpx` GET + RSC-payload regex; JSON API | Directory monthly days 8–12 07:15; SIF daily piggyback on NAV job | `webapp/amfi_portal.py` | `config/amc_registry.json` (URL fill), `data/reference/amfi_disclosure_members.json`, `data/parsed/sif/sif_latest_nav.json` |
| 5 | 57 AMC websites (monthly portfolio + factsheets) | Holdings with %NAV, sectors; factsheets; riskometer docs | Dedicated adapters: `httpx`, `curl_cffi` Chrome impersonation, Playwright, AES-encrypted JSON APIs, RSC/Next.js payload scraping | Monthly pipeline, days 1–5 06:00 IST (retry until success marker) + manual link-first re-download | `src/amc_adapters/*`, `src/amc_direct.py`, `src/pdf_downloader.py` | `data/raw/pdfs/<AMC>/<YYYY>/<MM>/`, `data/parsed/amc_websites/<AMC>/<YYYY>/<MM>/*.json`, `data/logs/portfolio_links/<YYYY-MM>.json` |
| 6 | `archives.nseindia.com/.../sec_bhavdata_full_<DDMMYYYY>.csv` + `nsearchives.nseindia.com/.../BhavCopy_NSE_CM_..._F_0000.csv.zip` | Daily OHLCV per symbol (2020+) | `urllib` GET (no cookies), cached per trading day | Daily 21:00 IST; full backfill manual | `src/stock_price.py:72,76` | `data/stock_history/<ISIN>.json`, cache `data/stock_bhavcopy/<YYYYMMDD>.csv` |
| 7 | `www.nseindia.com/api/historical/cm/equity` | Pre-2020 OHLCV history (1994→2019) | `curl_cffi` `impersonate="chrome124"` GET, warm NSE home, 1-year chunks, 1.5 s pacing | Manual phase-0 only (`--dump-nse-history`) | `src/stock_price.py:82` | `data/raw/nse_historical/<SYMBOL>.json` |
| 8 | `www.nseindia.com/api/corporate-announcements` | Announcements: dividends/splits/bonus, financial-result filings | `urllib` with cookie-warmed `nse_session()`, `Referer` NSE, retries=1 | Daily 21:00 IST | `src/stock_actions.py:46`, `src/stock_reports.py:26`, `src/financial_statements.py:48` | `data/stock_actions/<ISIN>.json`, `data/stock_reports/<ISIN>.json` |
| 9 | `www.nseindia.com/api/corporates-corporateActions` | Structured corporate actions (dividends, bonus, splits, rights) | same NSE session; latest ~20 rows only | Backfill/fill from dumps | `src/stock_actions.py:47` | `data/raw/nse_actions/<SYMBOL>.json` |
| 10 | `attchmntFile` PDFs from announcements | SEBI Reg-33 quarterly/annual results | `urllib` download, `%PDF-` validation; parsed with `pdfplumber`; AI vision tier (OpenRouter/opencode) only via bulk scripts | Weekly Sun 06:30 IST (stale-first, limit 12) + manual backfill | `src/financial_statements.py`, `scripts/pull_annual_results.py`, `scripts/download_results.py` | `data/stock_financials/<ISIN>.json`, PDFs `data/raw/financial_results/<SYMBOL>/` |
| 11 | `query1.finance.yahoo.com` (chart + events) | Price history range-fill; dividends/splits; crumb-protected | `urllib` with cookie jar; crumb from `/v1/test/getcrumb` | Daily (prices fallback + actions primary); event backfill flag-gated `STOCK_ALLOW_YAHOO=1` | `src/stock_price.py:87-89`, `src/stock_actions.py:44` | merged into `data/stock_history/<ISIN>.json`, dumps `data/raw/yahoo_actions/` |
| 12 | `www.google.com/finance/quote/<SYM>:NSE` | Latest close only (top-up) | `urllib` GET + regex `data-last-price` | Daily top-up (`daily=True` only) | `src/stock_price.py:90` | merged into `data/stock_history/<ISIN>.json` |
| 13 | `archives.nseindia.com/content/equities/EQUITY_L.csv` | NSE equity master (symbol ↔ ISIN) | `urllib` GET, latin-1 | On demand / first daily stock refresh | `src/stock_identity.py:32` | `data/stocks/identity.json` |
| 14 | `www.niftyindices.com/Factsheet/ind_<code>.pdf` | Index constituent weights (benchmark composition) | `httpx` GET PDF + `pdfplumber`; manual `--file` ingest for full-weight CSVs | Manual ingest per index cycle | `webapp/nifty_weights.py:83` | `data/nifty/weights.json` |
| 15 | NSE debt archives (3 bulk files) | Corporate-bond master, WDM trading list, CBM trades | `urllib` GET CSV, cached daily, 0.25 s pacing | Daily 21:30 IST | `src/bonds.py:68,73,79` | `data/bond_market/raw/<YYYY-MM-DD>/*.csv`, catalog `data/reference/bonds_catalog.json` |
| 16 | `www.nseindia.com/api/live-analysis-debt-market` | Live bond snapshot (price/ytm/rating) | cookie-warmed `urllib` GET | On demand + daily catalog build | `src/bonds.py:60` | `data/bond_market/live_debt_market.json` |
| 17 | `data/universe/navall.txt` (AMFI NAVAll snapshot) | Scheme-code dictionary: names, ISINs, latest NAV | No fetcher in-repo — R2-synced file (`universe/` prefix) | On R2 sync/startup | `src/amfi_nav.py:183` | read-only dictionary |
| 18 | nseindia.com historical debt constituents / local Nifty CSVs | Debt index constituents + Zerodha fund holdings (seed gap fill) | Local files | Manual corpus update | `src/bonds.py:56`, `src/index_resolver.py` | `data/nifty/debt_constituents/*` |
| 19 | `output/<SYMBOL>/nse_financials_latest5q.csv` | External worker's NSE results-comparision dump (last 5 periods) | Local file ingest, no network | Manual CLI | `src/ingest_output_financials.py` | merges into `data/stock_financials/<ISIN>.json` |
| 20 | `mfdata.in/api/v1` (aggregated holdings mirror) | AMC families + equity/debt holdings with `weight_pct` | `httpx` GET | **Retired from webapp scheduler**; still CLI `amfi-fetch` and `nav-daily` piggyback | `webapp/amfi_fetch.py:25` | `data/parsed/amfi/<stamp>_<slug>.json` |
| 21 | `api.mfapi.in/mf/{code}` (legacy NAV mirror) | Full NAV history per code | `urllib` GET, 0.25 s delay | Manual legacy utility, unscheduled | `src/fetch_missing_nav.py:38` | `data/nav_history/<code>.json` |
| 22 | capsolver.com / 2captcha.com | hCaptcha tokens | `httpx` POST/GET polling | On demand, **inactive/unwired** | `src/captcha_solver.py` | none |

---

## 2. Ways data points are obtained (technique inventory)

| Technique | Where used | Notes |
|---|---|---|
| stdlib `urllib` GET | AMFI NAV history, all NSE stock/debt calls, Yahoo chart/events, Google Finance, `api.mfapi.in` | TLS verification disabled globally (`src/stock_common.py:43`, `src/nav_history.py:76`) |
| `httpx` GET/POST | AMFI JSON APIs, TER, SIF NAV, AMC JSON APIs (PGIM, Choice, Bandhan, UTI, Mahindra), OpenRouter AI tier | Retry 3× on 5xx/transport; 4xx fails fast |
| Cookie-warmed NSE session | All `nseindia.com/api/*` calls | One warm-up GET to `https://www.nseindia.com/` then `CookieJar`; `retries=1` so Akamai blocks fail fast instead of hanging (`src/stock_common.py:61`) |
| `curl_cffi` Chrome impersonation | HDFC + Edelweiss AMC discovery/download (Akamai/Cloudflare WAF), NSE historical equity API | Bypasses TLS-fingerprint bot blocks |
| Playwright headless Chromium | JS-heavy AMC portals: Navi, Jio BlackRock, Union, Taurus, WhiteOak, Axis (Bearer token), NJ fallback, generic fallback | 3 s wait then JS link collection |
| AES-encrypted API payloads | ITI (AES-128-CBC), JM Financial, Mahindra (AES-256-CBC), Edelweiss (`Salted__`, EVP_BytesToKey) | Keys replicated from front-end JS bundles |
| Next.js / RSC payload scraping | AMFI portal directory, HDFC Next.js `__NEXT_DATA__`, WealthCompany escaped JSON, Bandhan WP API | Regex/RSC-unescape then JSON parse |
| Bulk archive files | NSE bhavcopy (legacy CSV + UDiFF ZIP), NSE debt master/WDM/trades | One file per trading day, cached; holidays naturally missing |
| Document parsing | `pdfplumber` (primary), PyMuPDF (fallback/text), Tesseract OCR (vector-outline/scanned), pandas (`openpyxl`) for Excel, BeautifulSoup/lxml for HTML, zip parser for bundles | sha256 parse cache avoids re-parse/re-bill |
| AI extraction (optional) | AMC factsheet vision (OpenRouter, `src/ai_extract.py`); statement vision (`scripts/pull_annual_results.py`; disabled in scheduled path) | Env-gated, cached by source sha256 |
| Human drop folder | `data/raw/manual_ingest/` — manual portfolio files | Ingested by `python main.py ingest` |
| Manual override CSVs | `data/raw/stock_manual/identity.csv`, `data/raw/stock_manual/<ISIN|SYMBOL>.csv` | Authoritative over all network sources for prices/identity |

---

## 3. Frequency at a glance

| Cadence | Sources |
|---|---|
| Twice daily | AMFI NAV history (23:30 + 08:30 IST) |
| Daily | NSE bhavcopy prices + actions + announcements (21:00), NSE bond files + catalog (21:30), AMFI SIF NAV (piggyback on NAV job), R2 thin-stub pre-heal (08:35) |
| Weekly | NSE result PDFs → financial statements (Sunday 06:30, stale-first limit 12) |
| Monthly (days 1–5) | AMC website portfolio/factsheet pipeline (retry until success marker) |
| Monthly (days 8–12) | AMFI registry directory verify (07:15), AMFI other-data suite (07:45), TER via CLI |
| On demand / manual | Full stock backfills, NSE pre-2020 history dump, Nifty weights ingest, annual-results waves, external CSV ingest, legacy mirrors |

Full cron/CLI detail: [cadence-matrix.md](cadence-matrix.md).

---

## 4. Fallback chains (summary)

- **Stock prices** (`src/stock_price.py`) — manual CSV → NSE bhavcopy (legacy CSV → UDiFF ZIP) → Google Finance (daily top-up only) → Yahoo chart (range/gap fill) → keep existing / `no_data`.
- **Corporate actions** — daily: Yahoo events → NSE announcements (supplementary) → keep previous. Backfill: NSE corp-actions dump → announcement keyword hits → Yahoo (flag-gated, only when NSE yielded zero).
- **NAV** — R2 full file first for cold-start, else AMFI portal full-history walk (capped 100/run); recent gaps via bulk AMFI window; deep gaps delegated to `backfill_codes_amfi`.
- **Holdings %NAV (webapp)** — source priority `amfi > amc_website > advisorkhoj > index`, then highest %NAV coverage; fallbacks: Nifty weights → computed from market value → equal weight.
- **Bonds** — merge priority: corporate master → WDM → CBM trades → live snapshot → local seeds; YTM: reported last yield → weighted-avg yield → computed → current yield.
- **Financial statements** — parsed-PDF records win over external CSV feed for the same period; AI tier only via bulk scripts.

---

## 5. Policy notes & known drifts

- `webapp/amfi_fetch.py` (mfdata.in) is **retired inside the webapp scheduler**
  (`skipped="mfdata retired from data-source policy"`, enforced by
  `tests/test_scheduler_wiring.py`) but still reachable via CLI `main.py amfi-fetch`
  and the `nav-daily` piggyback.
- `src/fetch_missing_nav.py` (api.mfapi.in) is live code but unscheduled; the
  stated policy is AMFI-only.
- `src/captcha_solver.py` is implemented but no adapter calls it (Kotak is served
  via the `kotak.bank.in` portal instead of its captcha-protected site).
- `yahoo_allowed()` in `src/stock_price.py` is dead code — Yahoo is attempted
  unconditionally there; only `stock_actions.backfill_empty_from_yahoo` honours
  `STOCK_ALLOW_YAHOO=1`.

> Candidate access layer (not yet integrated): Firecrawl as an AMC-website
> discovery fallback — see [`../FIRECRAWL_EVALUATION.md`](../FIRECRAWL_EVALUATION.md).
