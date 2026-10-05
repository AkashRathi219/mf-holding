# Data cadence — the two dates every scheme carries

For **every scheme** the system tracks two important dates:

1. **Latest NAV value date** — the date of the most recent daily NAV.
   - Refreshed **every day** (AMFI daily NAV feed → `data/nav_history/<code>.json`).
   - Surfaced in the Scheme Explorer drawer as **"Latest NAV date (daily)"** and on the
     portfolio holding statement as the **NAV date** column.

2. **Portfolio holdings announcement date** — the as-of date of the scheme's monthly
   portfolio disclosure (the date the holdings were published by the AMC).
   - Announcements are treated as a **weekly** update cycle.
   - Surfaced as **"Holdings as-of (weekly announcement)"** in the Scheme Explorer
     drawer and as the `as_of` on each scheme / holdings snapshot.

## Where each is stored

| Date | Field | Source | Cadence |
|---|---|---|---|
| Latest NAV | `nav_date` (scheme detail), `nav_history` last date | AMFI NAV history (`src.nav_daily`, `src.nav_freshness`) | Daily |
| Holdings announcement | `as_of` / `holdings_date` on the scheme | Parsed AMC monthly portfolio / advisorkhoj | Weekly (announcement) |

## Operations

- `python main.py nav-daily` — appends today's NAVs (daily refresh).
- `python main.py nav-backfill --cas|--all|--codes` — pulls NAVs from AMFI up to the
  latest date when a scheme's history is stale.
- `python main.py nav-status` — reports which schemes are complete from inception to
  the latest NAV date.
- `python main.py nav-freshness` — checks for stale NAVs/prices; `--backfill` refreshes them.

## AMFI other-data cadence (monthly, PLAN_AMFI_DATA_SOURCES)

Monthly jobs (scheduler `monthly_amfi_otherdata`, days 8-12 IST, after the AMFI
disclosure window; CLI `python main.py amfi-otherdata <job>` or
`python -m src.amfi_otherdata <job>`):

| Job | Data | AMFI endpoint | Cadence | Output |
|---|---|---|---|---|
| `mutual-funds` | MF directory + quarters + tracking months | page payloads (scheme-wise-disclosure, tracking-error) | Monthly | `data/reference/amfi_mutual_funds.json` |
| `tracking` | tracking error + tracking difference, all schemes | `/api/tracking-error-data`, `/api/tracking-difference` | Monthly | `data/reference/amfi_tracking.json` |
| `disclosure` | scheme-wise disclosure (SEBI 25-Aug-22) | `/api/schemewisedisclosure-investment` | Quarterly (job runs monthly, picks latest quarter) | `data/reference/amfi_scheme_wise_disclosure.json` |
| `risk-params` | SEBI risk parameters (large/small cap) | `/api/risk-parameter-data-revised` | Monthly | `data/reference/amfi_risk_parameters.json` |
| `aum` | AAUM fundwise + schemewise (AMFI_Code join), direct-plan bifurcation, state-wise | `/api/average-aum-*`, `/api/bifurcationaumdata`, `/api/statewise-data` | Monthly | `data/reference/amfi_average_aum.json` |
| `nfo` | new fund offers | `/api/new-fund-offer` | Monthly | `data/reference/amfi_nfo.json` |

`scheme-details` (scheme metadata, SSD documents, dividends) is a per-scheme fetcher
(`python -m src.amfi_otherdata scheme-details --mf <MF_ID> --scheme <scheme_id>`); the
bulk harvest across all 57 MFs is a later-phase job (see the plan tracker).

## Stock price cadence & endpoints (directive)

- **Primary:** sec_bhavdata_full_{DDMMYYYY}.csv from
  rchives.nseindia.com/products/content/ (per-symbol OHLC since ~2020),
  cached under data/stock_bhavcopy/.
- **Fallback (official archive):** UDiFF Common Bhavcopy ZIP
  BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip from
  
searchives.nseindia.com/content/cm/ - NSE deprecated legacy bhavcopy
  formats w.e.f. Jul-2024; _download_bhavcopy_day tries primary then UDiFF,
  and _parse_bhavcopy_day sniffs the format from the header row.
- **Daily chain:** manual CSV > bhavcopy > **Google** latest close (fast
  incremental top-up) > Yahoo ranged history. Full backfill skips Google and
  uses Yahoo for pre-2020 depth.
- **Corporate actions / announcements:** www.nseindia.com/api/* calls use the
  cookie-warmed session (stock_common.nse_session) with retries=1 so Akamai
  blocks fail fast instead of hanging the refresh for hours.
