# Project Audit — Incomplete Tasks & Priority Order

Status: AUDIT SNAPSHOT · Created: 23-Aug-2026
Related: `PLAN_TRY_APP.md` (approved build), `PLAN_PERFORMANCE_ANALYTICS.md` (queued),
`../APP_REVIEW_ACTIONS.md` (ops tracker), `../../DIRECTION.md` (master direction)

> Snapshot of a full project + webapp review at commit `3058f78` (working tree clean).
> Local webapp was not running at review time. All findings verified against code,
> with file:line references.

---

## State summary

Pipeline covers **97.8% of funds** (2,050/2,096); deploy kit works end-to-end.
Three real production bugs found, several half-shipped features, two approved-but-
unbuilt plans.

---

## 🔴 P0 — Broken in production (fix immediately)

| # | Issue | Evidence |
|---|---|---|
| 1 | **Scheduled NAV refresh silently dead.** Scheduler calls `nav_refresh_fn(days=10)` but `_nav_job()` takes no params → `TypeError` swallowed every run. Both daily jobs (`daily_nav_refresh`, `daily_nav_refresh_2`) do nothing; only manual superadmin triggers work. Live NAV data goes stale. | `src/scheduler.py:198` vs `webapp/main.py:84` |
| 2 | **Live Capsolver API key committed** in a git-tracked file. Rotate the key, move to env var, purge from history before any public repo. | `config/settings.yaml:85` |
| 3 | **`/api/version` always 500s** — `datetime` never imported inside `api_version()` (`NameError`). Ironically this is the stale-deployment detector. Trivial fix. | `webapp/main.py:700→707` |
| 4 | **Adviser feedback is lost on redeploys** — `feedback.json` written non-atomically to ephemeral disk, never synced to R2, write-only (no read endpoint even for superadmin). | `webapp/main.py:459` |

## 🟠 P1 — Approved plan + cheap wins

1. **Build PLAN_TRY_APP** (`PLAN_TRY_APP.md` — "APPROVED, ready to build"): public
   retail CAS-upload → report card → WhatsApp share loop. Nothing exists yet
   (`try_api.py`, `/api/try/*` absent). Blocked on: one real CAS PDF fixture +
   production URL. Its rate-limiter requirement doubles as the fix for item 2.
2. **No login/register rate limiting or token revocation** — brute-force /
   open-signup unmitigated (`webapp/auth.py`).
3. **Three fully-built screens are unreachable** — Proposal Generator, Portfolio
   Tools (overlap), API & Mapping have complete HTML+JS but aren't in the `screens`
   map, so `route()` falls back to Scheme Explorer (`webapp/static/js/app.js:3-9,65`).
   DIRECTION.md advertises these as features — wire them into nav or delete.
   Dashboard hiding was intentional (commit `2a59067`).
4. **Website ghost pages can't ship yet** — placeholder domains, stubbed waitlist
   JS, missing `POST /api/try/waitlist` endpoint, and no CORS for cross-origin
   `/api/scope-stats` (`website/*.html` TODOs).

## 🟡 P2 — Data completeness (mostly done; finish it)

- **Download backlog is nearly closed:** `data/reference/reconciled_active_download.csv`
  now holds just **2 funds** (HDFC Credit Risk Debt, UTI Credit Risk) — docs still
  say "~89/~209", update trackers.
- **discovery_needed.csv (46 funds):** 14 equity-Nifty ETFs resolvable *today* via
  existing `index_resolver`; the rest (~32 debt-index/commodity/BSE/MSCI/Nasdaq)
  need external index-weight sources — a sourcing decision, not code.
- **MF-A2:** >180d Advisorkhoj staleness computed server-side
  (`webapp/data_health.py:42`) but no user-facing badge — and `utils.js:57` actively
  masks the source name ("advisorkhoj" → "AMC disclosure"), contradicting
  transparency goals.
- MF-A4 universe name-matcher improvements; STK-A1 stock file backfill (minor).

## ⚪ P3 — Engineering debt (deferred items confirmed open)

- **P2#14 leftovers:** no FK cascades → deleting strategies/models/clients/
  portfolios orphans `analysis_runs` rows (`webapp/userdata.py` has zero FK
  constraints); NAV upsert uses `INSERT OR IGNORE` so revised AMFI NAVs are ignored
  forever (`src/nav_history.py:256`); `_norm_code` strips leading zeros.
- **Still-open analytics:** modified-duration metric for debt; overlap key fallback
  by issuer+coupon+maturity (`webapp/db.py:2521` keys by ISIN/name only).
- **Performance analytics engine** (`PLAN_PERFORMANCE_ANALYTICS.md` — queued): zero
  return math anywhere in codebase; the in-app feedback card shows users asking for
  it — consider promoting to P2.
- Hygiene: hardcoded marketing counts drifting (`register.html` vs live scope-stats),
  localhost curl examples, dead code (`_rationale`, drawer functions),
  sortable-header CSS with no sorting, `/api/models` CRUD unused by frontend, cached
  `analysis_runs` results unretrievable (metadata-only list endpoint).

---

## Recommended order

1. **P0 items 1–3 today** — small diffs, big risk reduction.
2. P0.4 + auth rate limiting.
3. Try App build (growth bet, unblocks website launch).
4. Orphaned-screen wiring + ghost-page publish.
5. Close final data gaps.
6. Debt items opportunistically.
