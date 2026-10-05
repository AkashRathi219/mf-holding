# Cadence Matrix — Jobs, Commands, Freshness

Single-page view of *when* every source is fetched. Sources referenced by
number/name in [README.md](README.md).

---

## 1. Scheduler jobs (`src/scheduler.py`, `config/settings.yaml`)

Times are IST (Asia/Kolkata, no DST). Global job defaults:
`misfire_grace_time=6h`, `coalesce=True`, `max_instances=1`. Webapp starts the
scheduler only when `ENABLE_SCHEDULER=1`.

| Job id | Schedule | What it runs | Outputs | Config |
|---|---|---|---|---|
| `monthly_holdings_fetch` | Days **1–5**, 06:00 (retries daily until `logs/success_<YYYY>-<MM>.marker`) | AMC registry pipeline: discover → download → parse | `data/raw/pdfs/**`, `data/parsed/amc_websites/**` | `scheduler.day_of_month/hour/minute/retry_days` |
| `daily_nav_refresh` | **23:30** daily | `nav_daily.update_latest_navs(days=10)` + gap fill; evening run carries AMFI piggyback (webapp: registry + SIF; CLI: legacy mfdata) | `data/nav_history/*.json`, `data/parsed/sif/sif_latest_nav.json` | `scheduler.nav_refresh` |
| `daily_nav_refresh_2` | **08:30** daily | Same NAV refresh, no piggyback (`args=[False]`) | `data/nav_history/*.json` | `scheduler.nav_refresh.hour2/minute2` |
| `daily_nav_preheal` | **08:35** daily | R2 thin-stub upgrade (`preheal_fn`; webapp wiring only) | `data/nav_history/*.json` | `scheduler.nav_preheal` |
| `daily_stock_refresh` | **21:00** daily | Identity → prices (bhavcopy/Google/Yahoo) → corporate actions → NSE announcements | `data/stock_history|actions|reports/**` | `scheduler.stock_refresh` |
| `daily_bond_refresh` | **21:30** daily | NSE debt bulk files + live snapshot → catalog with YTM | `data/bond_market/raw/**`, `data/reference/bonds_catalog.json` | `scheduler.bond_refresh` |
| `statements_refresh` | **Sunday 06:30** | Stale-first NSE result PDFs → statements (webapp limit 12) | `data/stock_financials/<ISIN>.json` | `scheduler.statements_refresh` |
| `monthly_amfi_fetch` | Days **8–12**, 07:15 | Webapp: AMFI registry verify (mfdata retired). CLI: legacy `run_amfi_monthly` | `config/amc_registry.json`, `data/reference/amfi_disclosure_members.json` | `scheduler.amfi_refresh` |
| `monthly_amfi_otherdata` | Days **8–12**, 07:45 | Tracking error/difference, disclosure, risk params, AUM, NFO | `data/reference/amfi_*.json`, raw `data/raw/amfi_otherdata/**` | `scheduler.otherdata_refresh` |

Scheduler heartbeat: records job ids + next wake-up to
`data/logs/refresh_state.json` (visible in `/api/health` and
`/api/admin/refresh-summary`).

---

## 2. CLI command map (`main.py`)

| Command | Fetches from | Notes |
|---|---|---|
| `run` | AMC websites | Full monthly pipeline |
| `parse-batch`, `parse`, `report`, `ingest`, `list-amcs` | local | Parsing/ingest utilities |
| `portfolio-links`, `portfolio-download` | AMC websites | Link-first monthly re-download |
| `nav-daily [--days]` | AMFI NAV | Daily incremental (+ legacy mfdata piggyback) |
| `nav-backfill --cas\|--all\|--codes` | AMFI NAV | Targeted histories |
| `nav-status`, `nav-freshness [--backfill]` | AMFI NAV / local | Coverage + freshness |
| `ter --month --year` | AMFI | TER Excel + reports |
| `amfi-fetch` | mfdata.in (legacy) | Holdings mirror |
| `amfi-directory [--dry-run]` | AMFI portal | Registry verification |
| `amfi-otherdata <jobs>` | AMFI JSON APIs | Monthly other-data |
| `sif-nav` | AMFI | SIF latest NAV |
| `stock-identity` | NSE archives | Force rebuild |
| `stock-price [--symbols --daily --limit]` | NSE bhavcopy et al. | Prices |
| `stock-actions`, `stock-reports` | NSE/Yahoo | Actions/announcements |
| `stock-refresh [--full]`, `stock-status` | NSE et al. | Daily orchestrator / report |
| `bond-refresh` | NSE debt | Files + catalog |
| `schedule-start` | — | Starts APScheduler daemon |

Module CLIs: `python -m src.nav_history|nav_daily|nav_freshness|nav_repair`,
`python -m src.amfi_otherdata`, `python -m src.stock_price|stock_actions|stock_identity`,
`python -m src.bonds`, `python -m src.ingest_output_financials`,
`python -m webapp.nifty_weights`, `python -m src.index_resolver`,
`python -m src.clean_equity_isins`, `python -m src.scheme_riskometer|scheme_descriptions|fund_managers|asset_class_breakup`.

Backfill scripts: `scripts/download_results.py`,
`scripts/pull_annual_results.py`, `scripts/run_pull_waves.py`,
`scripts/run_pull.ps1`, `scripts/close_stmt_gap.ps1`,
`scripts/audit_nav_freshness.py`, `scripts/resolve_scheme_codes.py`.

---

## 3. Manual / unscheduled operations

| Operation | Command | Cadence in practice |
|---|---|---|
| Full stock backfill & pre-2020 NSE dump | `python -m src.stock_price [--dump-nse-history] [--rebackfill-nse]` | Once / on corruption |
| Annual-results rebuild waves | `scripts/run_pull_waves.py` | Filing season |
| Nifty weights refresh | `python -m webapp.nifty_weights` | Per index review |
| Index holdings resolution | `python -m src.index_resolver` | When corpora update |
| Equity ISIN canonicalisation | `python -m src.clean_equity_isins` | When corpora update |
| Scheme metadata (riskometer/descriptions/FM/asset class) | module CLIs | Monthly cycle (runbook `docs/plans/PLAN_MONTHLY_REDOWNLOAD_ATTRIBUTES_FNO.md`) |
| Legacy NAV mirror fill | `python -m src.fetch_missing_nav` | Ad-hoc, policy caveat |
| External results CSV ingest | `python -m src.ingest_output_financials` | Ad-hoc |

---

## 4. Freshness / health expectations

| Pipeline | Max hours since last success | Source |
|---|---|---|
| `nav_daily` | 12 | `webapp/data_health.py:38` |
| `stock_refresh` | 48 | same |
| `bond_refresh` | 48 | same |
| `amfi_fetch` | 35 × 24 (monthly) | same |
| NAV / stock price file age bar | 10 days (`MAX_AGE_DAYS`) | `webapp/data_health.py:41` |
| Holdings disclosure freshness | as_of ≤ 45 days | `HOLDINGS_STALE_DAYS` |
| advisorkhoj snapshot flag | 180 days | `ADVISORKHOJ_STALE_DAYS` |
| Statements staleness | 35 days (`STALE_DAYS`) | `src/financial_statements.py:1396` |

Health score components: coverage 25, completeness 15, nav 20, holdings 15,
stocks_bonds 10, pipelines 13, stubs 2 (`webapp/data_health.py:31-33`).
