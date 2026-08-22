# App Review — Action Points & Status (updated 22-Aug-2026)

Legend: ✅ shipped · 🟡 partial · ⬜ open

## P0 — Broken flows

| # | Item | Status |
|---|---|---|
| 1 | CAS **JSON upload crash** (`import json` missing) | ✅ `tools_api.py` |
| 2 | **Client save crash** (missing `#mvClientNotes` input) | ✅ input restored; form relabelled |
| 3 | **Sector donut "Others" inflation** | ✅ uses `pfNormalize100` |

## P1 — Scoring / methodology

| # | Item | Status |
|---|---|---|
| 4 | Fake 100% compliance when zero rules evaluable | ✅ returns `null`; UI shows grey "N/A · no rules" |
| 5 | Unknown treated as zero on partial coverage | ✅ `_portfolio_resolved` gate → N/A |
| 6 | Max-single-holding rule ignored debt | ✅ evaluates largest effective holding of ANY class |
| 7 | Cross-user strategy-rule reads | ✅ `get_rules/set_rules` user-scoped |
| 8 | Mixed adjusted/raw stock closes + tz + blank bhav rows | ✅ split-factor splice (watermark-idempotent), UTC Yahoo dates, invalid closes skipped |
| 9 | Index resolver substring over-reach | ✅ whole-token matching (`_kw_hit`) |

## P2 — Display / polish

| # | Item | Status |
|---|---|---|
| 10 | Overlap heat scale unified + % cells | ✅ shared `overlapHeat()` |
| 11 | Allocation cap pie double-render legends | ✅ both use `pfPieFull100` |
| 12 | Donut hover dead-zone; bar NaN guards | ✅ charts.js fixed |
| 13 | Minor formatting (ytm 0-guard, coupon %, div-by-zero) | ✅ |
| 14 | Housekeeping | 🟡 feedback.json path ✅; FK cascade deletes ⬜ deferred; NAV revision-aware upsert ⬜; `_norm_code` leading-zeros ⬜ |

## Data-priority & completeness audit (22-Aug)

| Action | Status |
|---|---|
| MF-A1 Wire AMFI/mfdata tier-1 ingestion into CLI + scheduler; per-AMC files match db reader | ✅ `amfi-fetch` command + monthly job (days 8–12) |
| MF-A2 Flag stale Advisorkhoj-only schemes (>180d) in UI | ⬜ open |
| MF-A3 Reconcile ~209 active funds without coverage vs `reconciled_active_download.csv` | ⬜ open (data task) |
| MF-A4 Universe name-matcher improvements | 🟡 index-resolver boundaries fixed; fund-name matcher untouched |
| STK-A1 Backfill 1 non-fresh stock file; inspect 2 gap files | ⬜ open (minor) |
| BUG `stock-status --json` flag shadowed `json` module | ✅ renamed param |

## Deploy / accounts

| Action | Status |
|---|---|
| Railway deploy kit verified end-to-end; runbook `docs/DEPLOY_RAILWAY.md` | ✅ |
| `python-multipart` added to build requirements (boot crash) | ✅ bedbb91 |
| Accounts persist across redeploys: prepare_data stages `userdata.db`+`webapp_auth.db` (DBS list was unused); bootstrap pulls both | ✅ 3a35be5 |
| Demo accounts provisioned + seeded; junk test accounts purged from master DBs | ✅ |
| Samples slimmed: 1 strategy (Full Coverage Playbook) · 1 client (Rajesh) · CAS Sample Portfolio model+portfolio | ✅ c055a56/5beff44 |
| `/api/version` (live commit SHA) to detect stale deployments | ✅ 5beff44 |
| RIA/RA-flavoured labels: login/register screens + mandate/client/deploy forms | ✅ 5beff44 + register hero polish |
| Dashboard screen hidden; landing = Scheme Explorer; public `/api/scope-stats` + login data-scope grid with coverage badges | ✅ 2a59067 |
| Overview-tab Analyse rendering into hidden pane | ✅ container recreated inside visible pane |

## Bonds feature (new)

- NSE bulk-file ingestion (CBM master, WDM list, CBM trades) + live-API fallback ✅
- YTM engine: reported > computed (coupon+price+maturity, Excel-PRICE convention); trades-only rows resolved ✅
- Bonds tab with facets, coverage/error-vs-no-info card, fetched-on date, stale-price markers ✅
- Debt analysis + model-portfolio compliance consume catalog YTM ✅
- Daily scheduler job + `bond-refresh` CLI ✅

## Still open (next up)

1. ⬜ MF-A2 stale-scheme badge in UI
2. ⬜ MF-A3 ingest/reconcile the ~209 uncovered active funds
3. ⬜ Modified-duration metric for debt analysis
4. ⬜ Overlap key fallback by issuer+coupon+maturity (debt name variants understate overlap)
5. ⬜ Deferred P2 #14 housekeeping items (FK cascades, NAV revisions, code normalization)
6. ⬜ Operational: deletions made directly on the live site revert at next boot — re-upload snapshot after intentional cleanups (`prepare_data.py && upload_r2.py`)
