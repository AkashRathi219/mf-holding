# NAV History Pipeline — Source & Flow Map

Single upstream policy: **AMFI NAV portal only** for scheme NAVs. Third-party
NAV mirrors are legacy (see §8). Stocks have a separate price pipeline —
see [nse-stocks.md](nse-stocks.md).

---

## 1. Upstream

| Item | Value |
|---|---|
| Endpoint | `https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx` (`src/nav_history.py:56`) |
| Request | `?tp=1&frmdt=DD-Mon-YYYY&todt=DD-Mon-YYYY` |
| Windows | AMFI cap 90 days/request (`CHUNK_DAYS`); earliest `2006-04-01` (`START_DATE`) |
| Transport | stdlib `urllib` GET, gzip, TLS verify off, 180 s timeout |
| Retries | 5 attempts, `2**attempt` backoff; HTML/throttle pages 20 s/40 s extra; empty windows retried up to 5× before marked done |
| Politeness | 1.0 s between requests |
| Data points | scheme code, name, plan, option, ISIN growth, ISIN reinvestment, NAV, date |

Publication semantics (`src/nav_freshness.py`): day-T NAVs publish ~23:00 IST
(`AMFI_PUBLISH_CUTOFF`); weekend/holiday awareness via
`NSE_HOLIDAYS_2026` + override file `data/reference/nse_holidays_<year>.json`.

---

## 2. Modules

| Module | Role | Trigger |
|---|---|---|
| `src/nav_history.py` | Full since-inception backfill; resumable staging SQLite (`data/nav_history/.staging/nav.db`, WAL); export per-scheme JSON; `fetch_codes_history()` targeted fills | `python -m src.nav_history [--export] [--workers N --worker-id K]` |
| `src/nav_daily.py` | Daily incremental append (`update_latest_navs(days=10)`); cold-start fill; stale gap fill | Scheduler 2×/day + `python -m src.nav_daily` / `main.py nav-daily` |
| `src/nav_freshness.py` | Buckets (`current`, `lag1`, `stale_recent`, `stale_deep`, `dead_suspect`, `no_history`), backfills, completeness report | Invoked by gap fill; `main.py nav-freshness --backfill` |
| `src/nav_repair.py` | Re-fetch 90-day windows throttled to zero rows; revision-aware upsert into staging | `python -m src.nav_repair` |
| `src/nav_audit.py` | Freshness audit + three-way correctness sample (file ↔ live AMFI ↔ webapp.db) | `python -m scripts.audit_nav_freshness [--sample N] [--no-live --stocks]` |
| `scripts/resolve_scheme_codes.py` | NAVAll name→code resolution + optional NAV backfill | Manual, review CSV |
| `webapp/remote_store.py` | R2 mirror read/write (S3 API, boto3) for NAV/stock/reference | `ensure`, `download_to`, `ensure_prefix` |

---

## 3. Daily refresh flow (`nav_daily`)

1. Bulk AMFI fetch for `today − days … today` (default 10).
2. Merge into each `data/nav_history/<code>.json`, de-dupe by date, sort
   chronologically, stamp `fetched_at`.
3. **Stub policy:** a missing universe code is never seeded with a thin recent
   window. Instead:
   - try full file from R2 (`remote_store.ensure("nav_history/<code>.json")`), else
   - AMFI full-history walk, capped `MAX_FULL_HISTORY_FETCHES_PER_RUN = 100`
     with `PORTAL_WALK_DELAY = 1.0 s`.
4. `fill_gaps_from_last_known()`:
   - per-file stale scan (`max_age_days=6`),
   - shallow gaps (≤120 d): one bulk AMFI fetch from `min(last_known) − 3 d`,
   - deep gaps: `nav_freshness.backfill_codes_amfi(days=min(400, deepest+10))`,
     `deep_code_cap=400`.
5. Summary records `expected_latest`, created/updated/unchanged/skipped,
   `mirror_fetches`, `total_nav_points`; telemetry `nav_daily` / `nav_gapfill`.

Schedule: evening 23:30 IST (carries the AMFI/mfdata piggyback) and morning
08:30 IST (skips piggyback) — `config/settings.yaml:45-51`,
`src/scheduler.py:85-126`.

---

## 4. Backfill & repair

```
python -m src.nav_history                       # full 2006→today, resumable
python -m src.nav_history --workers 4 --worker-id K
python main.py nav-backfill --cas|--all|--codes # targeted backfills
python -m src.nav_repair                        # throttled windows
python main.py nav-status                       # completeness report
```

- Staging table `nav_history(scheme_code, date, nav, name, plan, option, isin,
  isin_re)` PK `(scheme_code, date)`; meta keys `chunk:<start>|<end>` /
  `chunkfail:*` for resumability.
- Revision-aware upsert: a corrected AMFI republication overwrites the old NAV.
- Export writes `manifest.json` + `download_summary.json` sentinels beside
  per-scheme files (freshness code ignores non-numeric stems).

---

## 5. Output schema & R2 mirror

`data/nav_history/<code>.json`:

```json
{
  "scheme_code": "...", "fund_name": "...", "category": "...",
  "plan": "...", "option": "...", "isin": "...", "isin_reinvestment": "...",
  "currency": "INR", "source": "AMFI", "fetched_at": "...",
  "history": [{"date": "DD-Mon-YYYY", "nav": 123.45}]
}
```

R2 (`db/nav_history/<code>.json`) is both a cold-start source and the webapp's
lazy heal path:

- `webapp/db.py` — thin-stub heal (`<30` points, `_heal_thin_nav_history`),
  `preheal_nav_stubs(limit=500)`, lazy `ensure` on first read;
- `webapp/market_value.py` — lazy `ensure` when building the latest-NAV index;
- `src/nav_daily.py` — full-file fill for missing codes;
- Scheduler pre-heal job 08:35 IST (webapp wiring only): upgrades thin
  cold-start stubs before the first visitor pays the cost.

Dates are stored `DD-Mon-YYYY`; all readers sort/compare via `_date_key`.

---

## 6. Freshness buckets (health view)

| Bucket | Meaning |
|---|---|
| `current` | 0 missed publish days |
| `lag1` | 1 missed publish day |
| `stale_recent` | 2–6 missed |
| `stale_deep` | 7–44 missed |
| `dead_suspect` | ≥45 missed |
| `no_history` | no file |

Completeness flags files COMPLETE when latest age ≤5 days and max internal gap
≤90 days. Health `< MAX_AGE_DAYS=10`.

---

## 7. SIF NAV (adjacent, AMFI JSON)

- `GET https://www.amfiindia.com/api/sif-latest-nav?type=<type>` →
  `data/parsed/sif/sif_latest_nav.json`, piggybacked on the daily NAV job
  (`webapp/main.py:154-161`); CLI `python main.py sif-nav`. See
  [amfi.md](amfi.md) §5.2.

---

## 8. Legacy NAV mirrors (policy caveat)

| Module | Endpoint | Status |
|---|---|---|
| `src/fetch_missing_nav.py` | `https://api.mfapi.in/mf/{code}` | Live code, but unscheduled; policy says third-party mirrors are retired |
| `webapp/amfi_fetch.py` (mfdata.in) | `https://mfdata.in/api/v1` | Holdings (not NAV); retired in webapp scheduler, still CLI-reachable |
| `src/nifty_isin_lookup.py` | docstring mentions live niftyindices.com fallback | Local-CSV only in implementation |

No functional NSE/Google fallback exists for MF NAV; the docstrings list them as
unimplemented hooks.
