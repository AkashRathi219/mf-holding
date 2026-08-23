# Execution Tracker — living status

One row per task. Machine-parsed by `scripts/tracker_status.py` (F2).
States: `todo · doing · blocked · review · done · dropped · parked`.
Commits tag task IDs: `fix(scheduler): ... [S2c]`. `Blocked` rows stale >7 d need a
`(revalidated DD-Mon)` stamp in Blocker. Plan: [`PLAN_EXECUTION.md`](PLAN_EXECUTION.md).

| ID | Task | Phase | State | Deps | Opened | Touched | Verify | Evidence | Blocker |
|----|------|-------|-------|------|--------|---------|--------|----------|---------|
| F1 | Task ledger seeded | F | done | — | 23-Aug | 23-Aug | — | this file | — |
| F2 | Stall detector script | F | done | F1 | 23-Aug | 23-Aug | scripts/tracker_status.py --selftest | selftest OK; ledger parses clean (47 rows) | — |
| F3 | Test scaffold (dev reqs, pyproject, conftest) | F | done | — | 23-Aug | 23-Aug | tests/conftest.py | requirements-dev.txt + pyproject.toml + sandbox fixtures | — |
| F4 | Smoke suite | F | done | F3 | 23-Aug | 23-Aug | tests/test_smoke.py | full GET sweep non-5xx, authed + anonymous | — |
| F5 | Scheduler wiring contract test | F | done | F3 | 23-Aug | 23-Aug | tests/test_scheduler_wiring.py | both wiring sites verified vs contract | — |
| F6 | Slim-dependency guard test | F | done | F3 | 23-Aug | 23-Aug | tests/test_slim_deps.py | 0 violations after amfi_nav decouple + S2a | — |
| F7 | CI workflow | F | done | F2, F4, F5, F6 | 23-Aug | 23-Aug | .github/workflows/ci.yml | YAML validated; runs on first push | — |
| F8 | Web-tier structured logging | F | done | — | 23-Aug | 23-Aug | tests/test_smoke.py::test_request_id_header | webapp/log.py + X-Request-ID middleware live | — |
| S0 | Confirm prod ENABLE_SCHEDULER + last NAV date | 1 | blocked | — | 23-Aug | 23-Aug | manual | — | needs Railway dashboard access |
| S1 | /api/version datetime fix | 1 | done | — | 23-Aug | 23-Aug | tests/test_smoke.py::test_version_endpoint | module-level datetime import; endpoint 200 | — |
| S2a | Add pyyaml/apscheduler/httpx to slim reqs | 1 | done | — | 23-Aug | 23-Aug | tests/test_slim_deps.py::test_allowlist_matches_requirements_slim_file | deploy/requirements-slim.txt updated | — |
| S2b | Startup cannot brick web tier | 1 | done | S2a | 23-Aug | 23-Aug | tests/test_scheduler_wiring.py::test_startup_resilient | guarded imports + try/except in hook + thread body logging | — |
| S2c | NAV job accepts days kwarg | 1 | done | S2a | 23-Aug | 23-Aug | tests/test_scheduler_wiring.py::test_nav_job_defaults_to_7_days | _nav_job(days=7), days passed to impl | — |
| S2d | pipeline_fn sync/async support | 1 | done | — | 23-Aug | 23-Aug | tests/test_scheduler_wiring.py::test_pipeline_fn_sync_ok | inspect.iscoroutinefunction branch + to_thread | — |
| S2e | AMFI job import guard | 1 | done | S2a | 23-Aug | 23-Aug | tests/test_scheduler_wiring.py::test_amfi_job_guarded | try/except around amfi_fetch import, error recorded | — |
| S2f | Scheduler heartbeat telemetry | 1 | done | S2b | 23-Aug | 23-Aug | tests/test_scheduler_wiring.py::test_heartbeat_recorded | record("scheduler","alive",jobs,next_wakeup) on start() | — |
| S3 | Health deep probe (503 on dead DB) | 1 | done | F8 | 23-Aug | 23-Aug | tests/test_smoke.py::test_health_deep_probe | db probe + r2/scheduler checks, 10s cache, 503 path tested | — |
| S4 | Feedback durability + admin reader | 1 | done | F8 | 23-Aug | 23-Aug | tests/test_feedback.py | atomic+locked writes, R2 restore/push, GET /api/admin/feedback | — |
| S5 | CapSolver env-var mechanism + runbook | 1 | done | — | 23-Aug | 23-Aug | tests/test_captcha_env.py | api_key blanked, api_key_env indirection, DEPLOY_RAILWAY runbook added | — |
| H1 | Login/register rate limiting | 2 | todo | — | 23-Aug | 23-Aug | tests/test_auth_hardening.py::test_rate_limit | — | — |
| H2 | Token revocation (token_version) | 2 | todo | — | 23-Aug | 23-Aug | tests/test_auth_hardening.py::test_revocation | — | — |
| H3 | CORS allowlist middleware | 2 | todo | — | 23-Aug | 23-Aug | tests/test_auth_hardening.py::test_cors | — | — |
| H4 | Secret caching + loud prod failure | 2 | todo | — | 23-Aug | 23-Aug | tests/test_auth_hardening.py::test_secret_hygiene | — | — |
| U1 | Orphaned screens wiring/deletion | 3 | parked | — | 23-Aug | 23-Aug | — | DECISIONS.md D1 | revisit at Try App launch / UX pass |
| U2 | Route fallback → 404 panel | 3 | todo | — | 23-Aug | 23-Aug | tests/test_routing_ui.py::test_unknown_hash | — | — |
| U3 | MF-A2 stale badge in UI | 3 | todo | — | 23-Aug | 23-Aug | tests/test_stale_badge.py | — | — |
| U4L | Mask leak fix in source filter (app.js:82) | 3 | todo | — | 23-Aug | 23-Aug | tests/test_source_masking.py | — | — |
| D1 | Close last 2 download-backlog funds | 4 | todo | — | 23-Aug | 23-Aug | data/reference/reconciled_active_download.csv empty | — | sourcing effort |
| D2 | Resolve 14 Nifty ETFs via index_resolver | 4 | todo | — | 23-Aug | 23-Aug | discovery_needed.csv delta | — | — |
| D3 | Sourcing decision doc (~32 index/commodity rows) | 4 | todo | — | 23-Aug | 23-Aug | docs/DATA_SOURCES_RESEARCH.md section | — | procurement decision needed |
| D4 | Correct ~89/~209 figures in trackers | 4 | todo | — | 23-Aug | 23-Aug | grep shows no stale counts | — | — |
| TRY1 | Try App Phases 1-3 build | 5 | blocked | H1, H3, S4 | 23-Aug | 23-Aug | PLAN_TRY_APP checklist | — | needs real CAS PDF fixture + production URL |
| WEB1 | Replace placeholders in website/*.html | 6 | blocked | — | 23-Aug | 23-Aug | zero TODO/YOURSITE hits | — | needs production domain |
| WEB2 | Waitlist endpoint POST /api/try/waitlist + R2 persist | 6 | todo | H3 | 23-Aug | 23-Aug | tests/test_waitlist.py | — | — |
| W1 | AMC Report Directory page from amc_registry.json | 6 | todo | — | 23-Aug | 23-Aug | scripts/build_amc_directory.py output | — | — |
| ANA1 | Scheme metrics engine (/analytics endpoint) | 7 | todo | — | 23-Aug | 23-Aug | metrics unit tests vs hand-computed | — | rf-source decision |
| ANA2 | Compare view (2-12 schemes) | 7 | todo | ANA1 | 23-Aug | 23-Aug | per plan checklist | — | — |
| ANA3 | Portfolio-level analytics | 7 | todo | ANA1 | 23-Aug | 23-Aug | per plan checklist | — | — |
| ANA4 | Proposal integration + disclaimer block | 7 | todo | ANA3 | 23-Aug | 23-Aug | per plan checklist | — | — |
| ANA5 | Methodology/marketing sync | 7 | todo | ANA1 | 23-Aug | 23-Aug | copy review vs guardrails | — | — |
| DBT1 | FK cascades in userdata.db | 8 | todo | — | 23-Aug | 23-Aug | delete-cascade tests | — | — |
| DBT2 | NAV revision-aware upsert | 8 | todo | — | 23-Aug | 23-Aug | revised-NAV test | — | — |
| DBT3 | _norm_code leading-zero preservation | 8 | todo | — | 23-Aug | 23-Aug | normalization tests | — | — |
| DBT4 | Overlap key issuer+coupon+maturity fallback | 8 | todo | — | 23-Aug | 23-Aug | debt-overlap tests | — | — |
| DBT5 | Modified-duration metric for debt | 8 | todo | — | 23-Aug | 23-Aug | duration tests | — | — |
| DBT6 | userdata.db user_id indexes | 8 | todo | — | 23-Aug | 23-Aug | EXPLAIN QUERY PLAN checks | — | — |
| DBT7 | Hygiene: dead code, stale webapp.db.tmp, hardcoded counts | 8 | todo | — | 23-Aug | 23-Aug | grep sweep clean | — | — |
