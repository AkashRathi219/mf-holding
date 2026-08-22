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

## Per-scheme confidence badge + reliance metrics (added 22-Aug)

**Scheme Explorer / scheme drawer** — every scheme now carries `confidence`
in its API payload (webapp/data_health.py `scheme_confidence()`):

- score = source base (amfi 100 · amc_website 88 · index 85 · advisorkhoj 62)
  − disclosure-age penalty (0 ≤35d … −48 >180d, MF-A2 territory), blended
  70/30 with holdings quality (ISIN% + %NAV coverage of its holdings rows);
- tiers: **high ≥80** green · **medium ≥55** amber · **low <55** red ·
  grey "no data" for no_disclosure/missing;
- hover tooltip shows source, disclosure age, ISIN coverage; the drawer also
  lists it as a Confidence row.
- `/api/schemes` enriches each page via one batched SQL aggregate
  (`WebDB.holdings_stats`, webapp/db.py); `/api/schemes/{id}` too.

**Superadmin Admin tab → "Data reliance" card** (`GET /api/admin/reliance`,
superadmin-gated): tier distribution across ALL schemes, avg score,
per-source table (schemes, avg score, stale >45d count, avg ISIN%), and a
clickable least-reliable-schemes list. Feeds remediation directly (MF-A2 /
MF-A3 backlog).

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

## Parsing optimization + AI tier (22-Aug, evening)

**Diagnosis first:** the proposed "false-negative text-layer detector" premise
was measured and rejected — ICICI digital factsheets have **0 fonts / 0 chars /
~1,859 vector drawings per page** (text drawn as glyph outlines). No detector
can recover text; OCR-on-render is genuinely required. The existing
`pdf_agents.py:901` check is correct.

Shipped instead:

| Phase | What | Verified result |
|---|---|---|
| 1. `src/batch_parser.py` | File-level `ProcessPoolExecutor` across PDFs; wired into `run --workers` + new `parse-batch` CLI | **141 ICICI docs in 543s @ 6 workers, 0 failed** (~4× vs sequential) |
| 2. sha256 parse cache (`metadata.source_sha256`) | Skip re-parse when source hash unchanged; legacy unstamped outputs stay valid; `--force` overrides | Re-run of the same batch: **543s → 1.4s** (141 cached) |
| 3. Geometry-aware OCR rows (`_ocr_pdf_tables`) | TSV word boxes → y-cluster lines → x-gap clusters → rightmost-% token; tolerates OCR-eaten decimals ("136%"→1.36); section/metric/sector/noise filters from pdf_agents | Row sums 300–2600% → **7–208%**; junk-name rows filtered |
| Validity gate (webapp/db.py `_src_score`) | Weight-invalid snapshots (>100 max / >120 sum) demoted by +2 source priority | Noisy OCR can no longer displace clean advisorkhoj weights on rank alone |
| AI tier (`src/ai_extract.py`) | OpenRouter vision extraction for outline/scanned PDFs; strict-JSON schema prompt; off unless `ai.enabled` + key env (`OPENROUTER_API_KEY`, or `AI_EXTRACT=1`); triggers only when heuristic rows <12 or weight-invalid; results ride the sha256 cache so each doc bills once | Offline paths tested (config-off, response parser); live call pending API key |

Ops notes:
- `parse-batch --amc X [--workers N] [--force]` re-parses downloaded PDFs in place.
- Enable AI: set `OPENROUTER_API_KEY` in env (+ optionally `AI_EXTRACT=1`);
  model/mode/dpi/trigger_rows configurable under `ai:` in settings.yaml.
- ICICI remains advisorkhoj-sourced by design until a *weight-valid* snapshot
  exists (AI tier is the intended path). Overall reliance after rebuild:
  green 999 · amber 1279 · red 35 · grey 200 — the green→amber shift vs the
  earlier run reflects invalid-weight amc_website snapshots being demoted to
  valid-weighted AK data (quality-first), not data loss.

### Confidence v2 — validation bonus (22-Aug, late)

The gate above exposed that ~1,000 advisorkhoj schemes carry *fully valid,
well-covered* holdings yet were amber-capped by source base alone, while some
old greens were weightless amc_website snapshots. Scoring updated in
`webapp/data_health.scheme_confidence`:

- **+10 validation bonus** when a snapshot is FRESH (≤45d), passes merge-time
  weight checks (max ≤100, sum ≤120) and has ≥90% coverage on both ISIN and
  %NAV — provenance matters less than proven usable data;
- stale or unvalidated snapshots keep pure source-based scoring; the DB-side
  demotion stays as the hard backstop.

Result: **High 1,879 (75%) · Medium 399 (16%) · Low 35 · No data 200 · avg 86.3**
— every High scheme has fresh, validated, weighted holdings regardless of
source label (`holdings_stats` now also returns `max_pct`/`sum_pct` so API
badges and the superadmin rollup score identically).
