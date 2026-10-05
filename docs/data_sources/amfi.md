# AMFI Data Sources

Primary regulator/industry sources: NAV history, registry directory, other-data
APIs, TER, SIF NAV. All AMFI endpoints are **unauthenticated GETs** (browser UA
+ per-endpoint `Referer` at most). No cookies, no API keys.

---

## 1. NAV history — `portal.amfiindia.com`

| Item | Value |
|---|---|
| Endpoint | `https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx` (`src/nav_history.py:56`) |
| Request | `?tp=1&frmdt=DD-Mon-YYYY&todt=DD-Mon-YYYY` |
| Library | stdlib `urllib.request`, TLS verification disabled, gzip decompressed |
| Limits | AMFI caps a query at 90 days (`CHUNK_DAYS`, `src/nav_history.py:58`); earliest date `2006-04-01` |
| Politeness | 1.0 s between requests; 5 retries with exponential backoff; HTML/throttle pages get 20 s/40 s extra backoff before treated as empty |
| Row fields | `Scheme Code; Scheme Name; Plan; Option; ISIN Growth; ISIN Reinvestment; NAV; Date` |
| Coverage | All schemes/all AMCs in one call per window |

### What consumes it

| Consumer | Behaviour | Frequency |
|---|---|---|
| `src/nav_history.py` | Full since-inception backfill into resumable staging SQLite `data/nav_history/.staging/nav.db`, then export per-scheme JSONs | Manual/backfill |
| `src/nav_daily.py` | `update_latest_navs(days=10)` appends recent points; cold-start codes get full histories from R2, else AMFI walk (cap 100 codes/run) | Daily, 23:30 + 08:30 IST |
| `src/nav_freshness.py` | Bulk recent-window backfill (`backfill_codes_amfi`) for stale/gapped files, guided by the AMFI publication calendar (day-T NAVs publish ~23:00 IST; NSE holiday set) | Daily via gap-fill / manual |
| `src/nav_repair.py` | Re-fetches 90-day windows that were throttled to zero rows | Manual |
| `src/nav_audit.py` | Three-way correctness sample: history file ↔ live AMFI ↔ `webapp.db` | Audit CLI |

Output schema (`data/nav_history/<code>.json`):

```json
{
  "scheme_code": "...", "fund_name": "...", "category": "...",
  "plan": "...", "option": "...", "isin": "...", "isin_reinvestment": "...",
  "currency": "INR", "source": "AMFI", "fetched_at": "...",
  "history": [{"date": "DD-Mon-YYYY", "nav": 123.45}]
}
```

Detail: [nav-history.md](nav-history.md).

---

## 2. NAVAll dictionary — `data/universe/navall.txt`

- Semicolon NAVAll snapshot: `Scheme Code; ISIN Growth; ISIN Reinvest; Name; NAV; Date`.
- **No downloader exists in-repo** — the file is shipped from Cloudflare R2
  prefix `universe` (`webapp/remote_store.py`, `deploy/bootstrap.py`).
- Parsed by `src/amfi_nav.py` (`get_nav()`, `fund_name_from_nav()`); used by
  `src/pdf_agents.py` for page→scheme detection, `src/amfi_ter.py`,
  `webapp/db.py`, `scripts/resolve_scheme_codes.py`.

---

## 3. Other-data API suite — `www.amfiindia.com/api/*`

`src/amfi_otherdata.py` (`BASE_URL = "https://www.amfiindia.com"`).
Transport: `httpx.Client`, Chrome UA, per-endpoint `Referer`, 3 attempts with
2·n s backoff (5xx/transport only), atomic JSON writes.

### Endpoints

| Function | Endpoint | Key params | Data points |
|---|---|---|---|
| `scheme_wise_disclosure` | `/api/schemewisedisclosure-investment` | `MF_ID`, quarter start `dd-MMM-yyyy` | Scheme/ISIN/company/security type, market value, **% to NAV** (SEBI 25-Aug-2022) |
| `tracking_error` | `/api/tracking-error-data` | `MF_ID`, `01-mmm-yyyy` | Scheme, benchmark, regular/direct tracking error |
| `tracking_difference` | `/api/tracking-difference` | `MF_ID`, 1st of month | Y1/Y3/Y5/Y10 tracking difference |
| `populate_scheme` | `/api/populate-scheme` | `MF_ID` | Scheme list (id + name) |
| `scheme_details` | `/api/scheme-details` | `MF_ID`, `scheme_id` | Objective, loads, type/category, launch, AUM |
| `scheme_documents` | `/api/schemes/{scheme_id}/documents` | path | SSD info/summary doc URLs (portal.amfiindia.com/spages) |
| `scheme_dividend(_years)` | `/api/years/scheme-dividend`, `/api/scheme-dividend` | `MF_ID`, scheme id, year | Dividend rate/plan/option history |
| `risk_parameters` | `/api/risk-parameter-data-revised` | `strCatId` 17=large-cap / 18=small-cap, `01-MMM-yyyy` | Stress test, concentration, volatility (SEBI risk params) |
| `new_fund_offers`, `nfo_detail` | `/api/new-fund-offer` | optional `Scheme_Id` | NFO groups/details |
| `average_aum_fundwise` | `/api/average-aum-fundwise` | `fyId`, `periodId` | AAUM per fund period |
| `average_aum_schemewise` | `/api/average-aum-schemewise` | `strType`, `MF_ID`, `fyId`, `periodId` | Scheme rows incl. `AMFI_Code` join key |
| `statewise_aum` | `/api/statewise-data` | `MF_ID`, `01-mmm-yyyy` | State × monthly AUM |
| `scheme_catwise_aum` | `/api/scheme-catwise-data` | `MF_ID`, date | Category × T15/T30 |
| `bifurcation_aum` | `/api/bifurcationaumdata` | `strdt` month-end | Direct-plan bifurcation |
| `agewise_folio` | `/api/aum-agewise-folio-report` | `Mon-YYYY` | Investor class × scheme type AUM/folios |
| `amc_investments` | `/api/investmentscheme` | `MF_ID`, quarter | AMC/sponsor holdings in own schemes |

Page-payload lookups (HTML, RSC-unescaped): `mutual_funds()` and
`disclosure_quarters()` from `/otherdata/scheme-wise-disclosure`;
`tracking_funds_and_months()` from `/otherdata/tracking-error`.

### Scheduler jobs (`python main.py amfi-otherdata <job>`, `python -m src.amfi_otherdata <job>`)

| Job | Output | Cadence note |
|---|---|---|
| `mutual-funds` | `data/reference/amfi_mutual_funds.json` | Monthly |
| `tracking` | `data/reference/amfi_tracking.json` (+ raw per MF/fund-month) | Monthly |
| `disclosure` | `data/reference/amfi_scheme_wise_disclosure.json` (+ raw per quarter) | Job monthly, data quarterly (latest quarter picked) |
| `risk-params` | `data/reference/amfi_risk_parameters.json` (large/small cap) | Monthly (steps back ≤4 months if empty) |
| `aum` | `data/reference/amfi_average_aum.json` | Monthly |
| `nfo` | `data/reference/amfi_nfo.json` | Monthly |
| `scheme-details` | `data/raw/amfi_otherdata/scheme_details/<mf>/<scheme>.json` | Per-scheme fetch (bulk harvest later phase) |

Scheduled: `monthly_amfi_otherdata`, days 8–12 at 07:45 IST
(`src/scheduler.py:240-261`, `config/settings.yaml:85-91`). Unwired fetchers:
`scheme_catwise_aum`, `agewise_folio`, `amc_investments`, `nfo_detail`
(selftest only).

---

## 4. TER — `src/amfi_ter.py`

| Item | Value |
|---|---|
| Months list | `GET /api/populate-ter-month?year=YYYY-YYYY` (`src/amfi_ter.py:39`) |
| Data export | `GET /api/populate-te-rdata-revised` with `MF_ID=All`, `Month=MM-YYYY`, `strCat=-1`, `strType=-1`, `excel=true` (returns whole-month file for all scheme types) |
| Referer | `https://www.amfiindia.com/ter-of-mf-schemes` |
| Fields | NSDL code, scheme name/type/category, TER date, Regular Plan Total TER %, Direct Plan Total TER % |
| Cadence | **CLI only** (`python main.py ter --month 07-2026 --year 2026-2027`), no scheduler |
| Outputs | `data/raw/ter/TER_<MM-YYYY>.xlsx`; `data/reference/ter_<month>_{schemes,universe,missing}.csv`; webapp cache `data/reference/ter_by_isin.json` |
| Matching fallbacks | Name aliases + containment matching + `MISSING_REASON` explanations |

---

## 5. AMFI portal — `webapp/amfi_portal.py`

### 5.1 Disclosure-member directory (registry verification)

- URL: `https://www.amfiindia.com/online-center/portfolio-disclosure`.
- Scrapes the React Server Component payload by regex → `{mf_id, mf_name,
  amc_name, monthly_url, fortnightly_url, half_yearly_url}`.
- `refresh_registry()` fills **only empty** `amc_monthly_portfolio_disclosure`
  URLs in `config/amc_registry.json` (curated URLs never overwritten; AMFI URL
  kept as `amfi_directory_url`), cache `data/reference/amfi_disclosure_members.json`.
- Cadence: days 8–12 at 07:15 IST (`monthly_amfi_fetch`) / CLI
  `python main.py amfi-directory [--dry-run]`.

### 5.2 SIF latest NAV

- URL: `GET https://www.amfiindia.com/api/sif-latest-nav?type=<type>` cycling
  `["", "Open Ended", "Close Ended", "Interval Fund"]`.
- Fields: SIF code, name, plan, option, ISINs, NAV, date.
- Output: `data/parsed/sif/sif_latest_nav.json` (+ best-effort R2 upload).
- Cadence: daily piggyback on the webapp NAV job / CLI `python main.py sif-nav`.

---

## 6. Holdings via third-party mirrors (legacy / policy-caveat)

### 6.1 `mfdata.in` — `webapp/amfi_fetch.py`

- Base `https://mfdata.in/api/v1`; `GET /families` and
  `GET /families/{fid}/holdings[?month=YYYY-MM]`.
- Returns equity + debt holdings with `weight_pct`; normalized to
  `{company, isin, percent_nav, market_value, sector, section}` and saved to
  `data/parsed/amfi/<stamp>_<amc-slug>.json` (`source="amfi"`, highest priority
  in `webapp/db.py`).
- Retries 3× (0/2/4 s), 4xx fails fast, `ProviderDown` on provider outage.
- **Policy status:** retired inside the webapp scheduler (records
  `skipped="mfdata retired from data-source policy"`; contract-tested). Still
  reachable via CLI `python main.py amfi-fetch` and the `nav-daily` piggyback.

### 6.2 `api.mfapi.in` — `src/fetch_missing_nav.py`

- `GET https://api.mfapi.in/mf/{code}` → full history for schemes referenced by
  `webapp.db` but missing a NAV file.
- Unscheduled legacy utility; conflicts with the stated AMFI-only NAV policy.
  Tagged `source: "AMFI"` in the output despite provenance.

### 6.3 `advisorkhoj` corpus (no fetcher in-repo)

- Consumed from `data/parsed/advisorkhoj/*.json` as a lower-priority holdings
  source (`amfi > amc_website > advisorkhoj > index`); the downloader is not in
  the current tree.

---

## 7. Telemetry (not an external source, but every AMFI run records to it)

`src/refresh_log.py` — append-only `data/logs/refresh_log.jsonl` + rolled-up
`data/logs/refresh_state.json` (R2-synced). Pipeline names: `amfi_fetch`,
`amfi_otherdata_*`, `nav_daily`, `nav_gapfill`, `scheduler` heartbeat. Feeds
`/api/admin/refresh-summary` and the data-health "pipelines" component.
