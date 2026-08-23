# Execution Plan — MF Holding Platform

Status: ACTIVE · Created: 23-Aug-2026 · Baseline: `main @ 3058f78`
Source backlog: [`PLAN_TASK_BACKLOG.md`](PLAN_TASK_BACKLOG.md) (frozen audit snapshot)
Living status: [`EXECUTION_TRACKER.md`](EXECUTION_TRACKER.md) · Decisions: [`../DECISIONS.md`](../DECISIONS.md)

Locked decisions: stabilize→growth strictly serial · thin test net first, grown per
workstream · markdown ledger + automated stall detector · CapSolver key inactive —
build the mechanism, not the panic · Advisorkhoj stays masked; AMC Report Directory
added as W1.

---

## 0. Corrected diagnosis (read before executing)

Three findings not in the original audit materially change P0 #1.

**(a) The scheduler cannot start at all — the signature bug is unreachable.**
`_start_scheduler_thread()` imports `yaml` (`webapp/main.py:125`) and `apscheduler`
via `src/scheduler.py:8-9` (`webapp/main.py:127`) — **neither is in
`deploy/requirements-slim.txt`** (fastapi, uvicorn, pydantic, boto3,
python-multipart, pdfplumber). Those imports run on the **calling thread**, before
`threading.Thread(...)` at `:152`, inside an **unguarded** `@app.on_event("startup")`
(`:156-158`).

Consequence: with `ENABLE_SCHEDULER=1` the lifespan raises `ModuleNotFoundError`,
uvicorn exits, `/api/health` never answers, Railway crash-loops 3× and gives up.
**The deployment is only alive today because the scheduler is switched off.**
Fixing `src/scheduler.py:198` alone changes nothing; adding the deps alone boots the
scheduler and *then* detonates the signature bug at 20:30 IST. P0 #1 is a five-part
fix. Verify `ENABLE_SCHEDULER` in the Railway dashboard before anything else (S0).

**(b) Two more latent breakages on the same path.** `src/scheduler.py:179` does
`await self.pipeline_fn(...)` against the synchronous `_noop_pipeline`
(`webapp/main.py:110`) → `TypeError` every monthly run. And `_amfi_job` is hard-dead
under slim deps: unguarded `httpx` import at `webapp/main.py:43` →
`webapp/amfi_fetch.py:21`. The AMFI SIF sub-step of `_nav_job` is silently dead for
the same reason (`webapp/main.py:100-106` swallows it into `meta["sif_error"]`).

**(c) `/api/health` is a static literal** (`webapp/main.py:693-694`) returning
`{"status":"ok"}` with no DB or R2 probe — yet `railway.json:11` uses it as the
deployment gate. A container with a missing `webapp.db` deploys green.

**Reclassification:** audit-P0#2 (CapSolver key) drops to hygiene — key inactive, so
we build the env-var mechanism and an activation runbook (S5), no rotation fire-drill.
Its slot in P0 is taken by findings (b)/(c).

---

## 1. Phase structure

Serial gates. A phase may not start until the prior phase's exit criteria are green
in the tracker.

| Phase | Theme | Est. | Gate to exit |
|---|---|---|---|
| **F** | Foundation: tracker, tests, CI, logging | 2 d | CI green; tracker detector runs clean |
| **1** | P0 stabilization | 3 d | All P0 tests pass; scheduler heartbeat visible in prod for 48 h |
| **2** | Security hardening | 2 d | Rate-limit + revocation + CORS tested |
| **3** | Transparency & routing hygiene | 2 d | 404 fallback live; MF-A2 badge live; source masking consistent |
| **4** | Data completeness | 1 d + sourcing | Backlog CSVs closed or formally deferred with owner |
| **5** | Try App (growth) | 5 d | PLAN_TRY_APP verification checklist |
| **6** | Website publish (+W1 directory) | 2 d | Zero placeholders; waitlist persists a real row; directory generated & linked |
| **7** | Performance analytics | 8 d | Per-phase checklist in PLAN_PERFORMANCE_ANALYTICS.md |
| **8** | Engineering debt | opportunistic | — |

### Phase F — Foundation (enabler)

Every P0 bug is a class of bug a 20-line test would have caught: a signature
mismatch, a missing import, a missing dependency. Build those tests first, then fix
the bugs — otherwise they recur.

| ID | Task | Detail |
|---|---|---|
| F1 | Task ledger | `docs/plans/EXECUTION_TRACKER.md` — machine-parseable, seeded with every ID in this plan |
| F2 | Stall detector | `scripts/tracker_status.py` (§2.2) |
| F3 | Test scaffold | `requirements-dev.txt` (pytest, pytest-cov, ruff, httpx), `pyproject.toml`, `tests/conftest.py` with synthetic SQLite fixtures for the three DBs |
| F4 | Smoke suite | `tests/test_smoke.py` — app imports; every registered GET route returns non-5xx against seeded fixtures; health/version endpoints |
| F5 | Wiring contract test | `tests/test_scheduler_wiring.py` — `inspect.signature` compatibility between every callable `MonthlyScheduler` invokes and every callable passed to it from `webapp/main.py:139-145` and `main.py:1289-1293`; asserts async/sync correctness too. Permanent fix for the P0#1 bug class |
| F6 | Slim-dependency guard | `tests/test_slim_deps.py` — AST-walk the import graph reachable from `webapp.main` (including the `ENABLE_SCHEDULER=1` path); assert no unguarded third-party import outside `deploy/requirements-slim.txt`. Permanent fix for the deploy-brick class |
| F7 | CI | `.github/workflows/ci.yml` — ruff + pytest + `tracker_status.py --ci` (non-zero exit on RED) |
| F8 | Web-tier logging | `webapp/log.py` — the `webapp/` package currently has zero `import logging`; production failures are invisible beyond uvicorn's access log. Structured JSON to stdout, request-id middleware beside `_no_cache_static` (`webapp/main.py:161`); replace highest-value `except Exception: pass` sites with `log.warning` |

### Phase 1 — P0 stabilization

| ID | Task | Change |
|---|---|---|
| S0 | Confirm prod scheduler state | Read `ENABLE_SCHEDULER` in Railway; record actual last-successful NAV date *(manual — needs dashboard access)* |
| S1 | `/api/version` 500 | `webapp/main.py:700` → `from datetime import datetime, timedelta, timezone` |
| S2a | Deploy deps | Add `pyyaml`, `apscheduler`, `httpx` to `deploy/requirements-slim.txt` |
| S2b | Startup must not brick | Wrap `_start_scheduler_thread()` call (`webapp/main.py:157`) + thread body in try/except with logging. A dead scheduler must degrade, never take the web tier with it |
| S2c | Signature mismatch | `_nav_job(days: int = 7)` (`webapp/main.py:84`) accepting and honouring `days`, passed to `_update_latest_navs_impl(days=days)` at `:95` |
| S2d | Async/sync mismatch | `src/scheduler.py:179` — support sync `pipeline_fn` via `asyncio.to_thread` like its siblings (keep async support for CLI wiring) |
| S2e | AMFI job dead | `webapp/main.py:43` unguarded `httpx` import — resolved by S2a; add guard as defence-in-depth |
| S2f | Scheduler heartbeat | `refresh_log.record("scheduler","alive", jobs=[...])` on `start()` incl. per-job `next_run_at`, surfaced via `/api/admin/refresh-summary`. Without this, "silently dead" recurs — nothing distinguishes *never ran* from *never scheduled* |
| S3 | Health deep probe | `/api/health` (`webapp/main.py:692`) → probe `webapp.db` readable + non-empty, report R2 config + scheduler heartbeat age; 503 on hard fail so Railway's gate is real. Cache 10 s, p99 < 50 ms |
| S4 | Feedback durability | `webapp/main.py:459` → pull `logs/feedback.json` via `remote_store.ensure` before read; atomic tmp+replace write; `threading.Lock`; `upload_object` push (pattern from `webapp/data_health.py:231-247`). Add superadmin `GET /api/admin/feedback` — feedback was write-only, leaving loop L3 without a reader |
| S5 | CapSolver mechanism | `config/settings.yaml` captcha block → `api_key: ""` + `api_key_env: CAPSOLVER_API_KEY`; `src/captcha_solver.py` reads env (mirrors `ai.api_key_env`, `settings.yaml:25`). Activation runbook section in `docs/DEPLOY_RAILWAY.md`. No rotation/purge — key inactive |

**Exit gate:** 48 h of prod telemetry showing `nav_daily` success at both 08:30 and
20:30 IST plus a `scheduler alive` heartbeat.

### Phase 2 — Security hardening

| ID | Task | Detail |
|---|---|---|
| H1 | Rate limiting | In-process per-IP sliding window on `/api/auth/login` + `/api/auth/register` (`webapp/main.py:215,223`). DoS asymmetry: each attempt runs PBKDF2 at 200k iterations (`auth.py:28`). Build as reusable middleware — PLAN_TRY_APP needs the same limiter for `/api/try/analyze` |
| H2 | Token revocation | `users.token_version` column + claim in `_make_token` (`auth.py:82`), checked in `verify_token` (`auth.py:93`); bump on password change / logout-all. Today a leaked token is valid the full 7 days (`auth.py:27`) with no kill switch |
| H3 | CORS | No CORS middleware exists anywhere. Env-driven allowlist. Blocks Phase 6 — marketing site is cross-origin from the app |
| H4 | Secret hygiene | Cache `_get_secret()` (currently a disk read per request); fail loudly when `SECRET_KEY` unset in prod instead of silently auto-generating per process (`auth.py:50-54`) |

### Phase 3 — Transparency & routing hygiene

| ID | Task | Detail |
|---|---|---|
| U1 | Orphaned screens — **PARKED** | Four fully-built screens are unrouted (`app.html:41/:236/:301/:369`; init fns `app.js:102,1271,1388,2284`; absent from screens map `app.js:3-9` and nav `app.html:13-21`). Decision D1: **do nothing now** — parked on review list; revisit at Try App launch or next UX pass. Dashboard hiding stays intentional (`2a90567`) |
| U2 | Silent route fallback | `app.js:65` swallows any unknown hash into Scheme Explorer; render a 404 panel instead — this is what hid U1 |
| U3 | MF-A2 stale badge | `data_health.py:130-142` computes `amc_disclosure_archive_stale_gt_180d` (superadmin-only); surface a per-scheme badge using age tiers (`data_health.py:312-323`) — staleness only, never names the source |
| U4L | Source-masking leak fix | Decision D2: masking stays policy (`utils.js:57` maps `advisorkhoj`→"AMC disclosure"). Fix the one leak: `app.js:82` renders raw `advisorkhoj` as a filter option — route through `App.sourceLabel`. Attribution question closed |

### Phase 4 — Data completeness

D1 close the 2 remaining funds in `reconciled_active_download.csv` (HDFC Credit
Risk Debt, UTI Credit Risk) · D2 resolve the 14 Nifty ETFs in
`discovery_needed.csv` (46 rows) via existing `index_resolver` · D3 sourcing
decision doc for the ~32 BSE/MSCI/Nasdaq/commodity rows (procurement, not code) ·
D4 correct stale "~89/~209" figures in `APP_REVIEW_ACTIONS.md:40` and `DIRECTION.md`.

### Phases 5–8 (gated)

**5 Try App** — per `PLAN_TRY_APP.md`; inherits H1, H3, S4, F8. Blocked on: one real
CAS PDF fixture + production URL. **6 Website** — 17 TODOs / 39 `YOURSITE.com` /
16 `YOURAPP.railway.app` across three files; `investors.html:394-400` waitlist is a
fake-success stub that discards every email — build `POST /api/try/waitlist` +
persist to R2. **R1 Rebrand** — Factsheet Engine AI → **FundPulse**
(`docs/NAMING_BRAND_ARCHITECTURE.md` §4 checklist); execute before WEB1 so the
ghost pages ship under the final name and subdomain. **W1 AMC Report Directory** — public static page listing every AMC →
official site + factsheet/portfolio-disclosure/scheme-wise download links,
generated by a build script from `config/amc_registry.json` (maintained by the
monthly AMC-direct link-capture pipeline, commit `3058f78`); empty URL fields
rendered as "—"; zero backend; regenerated each monthly capture; cross-linked from
all three ghost pages. **7 Analytics** — per `PLAN_PERFORMANCE_ANALYTICS.md`;
promotion evidence comes from loop L3 once S4 lands. **8 Debt** — FK cascades
(`userdata.py` has zero constraints), NAV `INSERT OR IGNORE` revision blindness
(`nav_history.py:256`), `_norm_code` zero-stripping (`:84`), overlap
issuer+coupon+maturity fallback (`db.py:2521`), modified duration, `userdata.db`
missing `user_id` indexes, stale 41.5 MB `data/webapp.db.tmp`.

---

## 2. The tracker

### 2.1 Ledger — `docs/plans/EXECUTION_TRACKER.md`

One row per task, machine-parseable. `PLAN_TASK_BACKLOG.md` stays frozen as the
23-Aug audit snapshot; the tracker is the living state.

```
| ID | Task | Phase | State | Deps | Opened | Touched | Verify | Evidence | Blocker |
|----|------|-------|-------|------|--------|---------|--------|----------|---------|
| S2c | NAV job signature | 1 | doing | S2a | 23-Aug | 24-Aug | tests/test_scheduler_wiring.py::test_nav_job_accepts_days | — | — |
```

States: `todo` · `doing` · `blocked` · `review` · `done` · `dropped` · `parked`.
Commit convention: `fix(scheduler): ... [S2c]` — how the detector correlates claims
with reality.

### 2.2 Stall detector — `scripts/tracker_status.py`

Parses the ledger, cross-references `git log`, optionally runs each done row's
verify target.

| Signal | Rule |
|---|---|
| RED STUCK | `doing` > 3 days with no commit tagged `[ID]` since Touched · `blocked` > 7 days unless Blocker carries `(revalidated DD-Mon)` within 7 d · `review` > 2 days |
| RED FALSE-DONE | state `done` but its verify target fails — catches silent regression after completion |
| ORANGE STARVED | `todo`, all deps `done`, phase active, untouched > 5 days |
| ORANGE GATE-BREACH | task in phase N+1 active while phase N has open rows — enforces serial sequencing |
| ORANGE REVIEW-LIST | `parked` > 30 days — parking must not become forgetting |

Output: console table + `data/logs/tracker_status.json`. `--ci` exits non-zero on
any RED. Registering as a weekly APScheduler job is deliberate *after* Phase 1
proves the scheduler works — not before.

---

## 3. Success metrics

**Tier 1 — System SLIs** (source: `refresh_log.summary()`, `data_health.jsonl`)

| Metric | Definition | Today | Target |
|---|---|---|---|
| NAV freshness | Age of newest NAV vs last trading day | unknown/stale | ≤ 1 trading day, 99% of days |
| Scheduled-job success | successes ÷ scheduled runs, 30 d | 0% (scheduler never starts) | ≥ 95% |
| Scheduler liveness | Heartbeat age (S2f) | no signal | < 25 h |
| Data-health score | `data_health.py` weighted composite | baseline at Phase 1 | ≥ 85, non-declining 30 d |
| Boot success | Deploys serving traffic ÷ attempted | health check is a literal | 100%, with a real probe |
| API 5xx rate | 5xx ÷ requests | unmeasurable (no logging) | < 0.1% |
| Scheme coverage | `has_holdings` ÷ active universe | 97.8% | ≥ 99% |
| Stale advisorkhoj | `>180d` count (`data_health.py:142`) | tracked, unsurfaced | declining, visible |

**Tier 2 — Delivery** — throughput (tasks done/week), stuck count (target 0 at each
weekly review), lead time by phase, false-done count (always 0), test count and
boot-path coverage.

**Tier 3 — Product (Phase 5+)** — Try App funnel (visit→upload→report→share),
share-link CTR, waitlist conversion, adviser WAU, feedback volume per feature.

**Tier 4 — Data quality** — confidence distribution across `SOURCE_BASE` tiers
(`data_health.py:271`), discovery backlog burn-down, NAV gap-fill volume, AMC
directory freshness (% AMCs with ≥1 live link; target ≥95%, regenerated monthly).

---

## 4. Feedback loops

| Loop | Cadence | Mechanism | Acts on |
|---|---|---|---|
| L1 Machine health | daily | `refresh_log` + `data_health.jsonl` digest via deep probe; alert on SLI breach | Tier 1 |
| L2 Deploy | per deploy | `/api/version` (S1) + deep `/api/health` (S3) + post-deploy smoke subset of F4 | boot success |
| L3 User | weekly | Durable feedback (S4) + `GET /api/admin/feedback`; triage into ledger as new IDs — this is what promotes Phase 7 on evidence rather than intuition | Tier 3 |
| L4 Delivery | weekly | `tracker_status.py` review: clear REDs, re-scope or drop anything stuck twice | Tier 2 |
| L5 Data | monthly | Coverage + confidence trend vs backlog CSVs; sourcing decisions; regenerate W1 directory | Tier 4 |
| L6 External | Phase 6+ | Search Console + share-link telemetry on ghost pages; directory → trial-page CTR | Tier 3 |

L3 is the one that doesn't exist today — feedback is write-only to an ephemeral
disk. S4 is its prerequisite, which is why it sits in Phase 1.

---

## 5. Future work (beyond this plan)

Deliberately out of scope, recorded so they aren't rediscovered: move the scheduler
out of the web container into Railway cron or a dedicated worker service (the
in-process daemon thread is the root cause of this entire P0 class); versioned DB
migrations instead of hand-rolled PRAGMA/ALTER; split `webapp/db.py` (3,062 lines)
and `tools_api.py` (1,207); pin dependencies and reconcile the two requirements
files; error tracking (Sentry-class); telemetry in a durable table instead of
ephemeral JSONL; DPDP/SEBI items in `docs/internal/`; multi-worker-safe locking
(`_admin_running` and in-process rate limiters are per-process only).

---

## 6. Decisions record

Both open decisions resolved 23-Aug-2026 — see `docs/DECISIONS.md`:
**D1** orphaned screens parked on the review list; **D2** Advisorkhoj masking stays
policy + W1 AMC Report Directory added. No open questions remain.
