# Data Ops & Health Score — Plan / Report (22-Aug-2026)

## Problem observed (superadmin Admin screen)

All four refresh pipelines showed **"no runs"** even though the jobs are wired:

| Pipeline | Wrapper | Why it looked empty |
|---|---|---|
| `nav_daily` | `track("nav_daily")` webapp/main.py:78, src/nav_daily.py:62 | Telemetry only lives in `data/logs/refresh_log.jsonl` on the container's **ephemeral disk** — wiped on every Railway redeploy; never staged to R2 |
| `amfi_fetch` | `track("amfi_fetch")` webapp/main.py:42 | same |
| `bond_refresh` | `track("bond_refresh")` webapp/main.py:53 | same |
| `stock_refresh` | `track("stock_refresh")` src/stock_refresh.py:20 | same |

Secondary: `ENABLE_SCHEDULER` was unset → "scheduler OFF" → nothing runs unless
someone clicks **Run now**.

## Phase 0 — Durable "last fetched" records (shipped)

1. **`data/logs/refresh_state.json`** — new compact per-pipeline rollup
   (`last_started`, `last_success`, `last_error`, `last_duration_s`,
   `last_detail`, counts), atomically rewritten by `src/refresh_log.record()`
   alongside the JSONL.
2. **R2 push-back** — `webapp/remote_store.upload_object()` (new); after each
   recorded event `refresh_state.json` is best-effort uploaded so the last
   fetched state survives redeploys. Requires R2 credentials with write access.
3. **Boot restore** — when the state file is absent locally (fresh container),
   `refresh_log.read_state()` pulls it back from R2 (`ensure("logs/…")`);
   `summary()` merges state-file values into the JSONL rollup so Last run /
   Took / error survive redeploys.
4. **Deploy kit** — `deploy/prepare_data.py` now stages
   `data/logs/refresh_state.json` (SKIP_DIRS excludes `logs` for dirs, the
   explicit file entry bypasses that).
5. **Ops** — `ENABLE_SCHEDULER=1` is set on Railway ✅; R2 token verified with
   write+delete access ✅ (scratch-key probe) and R2 seeded with the current
   `logs/refresh_state.json`, so the deployed instance starts with real
   last-fetched history before its first scheduled run.

## Phases 1–4 — Data Health Score

Composite 0–100 score + per-component breakdown, superadmin-only.

### Phase 1 — `webapp/data_health.py`
| Component | Weight | Signal | Source |
|---|---|---|---|
| Coverage | 25 | % schemes with holdings; discovery_needed half-credit; no_disclosure excluded from denominator | `db.meta_stats().coverage_dist` (webapp/db.py:1899) |
| Completeness | 15 | ISIN completeness % | meta_stats |
| NAV freshness | 20 | share of nav_history files with latest value ≤ 10 days old (reuses `stale_days()` logic) | src/nav_freshness.check_navs |
| Holdings freshness | 15 | schemes table `as_of`: median age + % older than 45 days (single SQL query); Advisorkhoj-source staleness sub-signal (MF-A2) | SQLite over `schemes` |
| Stocks/Bonds | 10 | stale stock files ≤ 10 days + bond catalog YTM/price coverage | src/nav_freshness.check_stocks + `_bond_catalog()` (webapp/db.py:2167) |
| Pipelines | 15 | err_24h + last-success age vs expected cadence (nav/stock/bond daily ≈ 48 h grace, amfi monthly ≈ 35 d) | `refresh_log.summary()` + refresh_state |

Bands: green ≥ 90 · amber 70–89 · red < 70.

### Phase 2 — API
`GET /api/admin/data-health` behind `_require_superadmin()` (webapp/main.py:686),
~5-minute TTL cache; invalidated when an admin pipeline run finishes.

### Phase 3 — Admin screen UI
Health card atop `initAdmin()` (webapp/static/js/app.js:2312): overall badge,
per-component bars, failing components link to Run-now actions.

### Phase 4 — History
Each fresh computation appends a snapshot to `data/logs/data_health.jsonl`
(same R2 push treatment as refresh_state).

## Verification (22-Aug-2026)
- `compileall` / AST clean across `src`, `webapp`, `deploy`; `node --check app.js` OK.
- Telemetry round-trip: `record()` → `refresh_state.json` written → `summary()`
  merges it; newest-success-wins regression fixed (legacy naive-IST timestamps
  no longer beat newer IST-offset events).
- Local compute result: **overall 87.9 (amber)** — coverage 99.4, completeness
  98.9, NAV 96.8, holdings 99.1, stocks/bonds 64.9 (bond YTM coverage ~30%),
  pipelines 50 (only nav_daily has local run history).
- API smoke (TestClient): `/api/admin/data-health` 200 · `-history` 200 ·
  `refresh-summary` exposes `state_file` · non-superadmin → 403.
