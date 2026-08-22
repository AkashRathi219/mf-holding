# Railway Deployment & Redeploy Runbook

The app deploys to **Railway** from this Git repo; all runtime data lives in a
**Cloudflare R2** bucket (S3-compatible) — the repo never carries data.

```
GitHub (main)  --push-->  Railway (auto-deploy)
                             | boot: deploy/bootstrap.py
                             v
                       R2 bucket (db/…)  <---push---  deploy/upload_r2.py
                             ^                              ^
                       lazy per-request pulls        local: prepare_data.py
                       (webapp.remote_store)         + upload_r2.py
```

## Components

| Piece | File | Role |
|---|---|---|
| Stage runtime data | `deploy/prepare_data.py` | copies only the files the webapp reads into `deploy/data/` + writes `deploy/manifest.json` (sha256 per file). Re-runnable. |
| Push to R2 | `deploy/upload_r2.py` | uploads staged files under the `db/` prefix; **idempotent** (skips unchanged by size+md5); manifest uploaded last. Credentials from `deploy/.env`. |
| Boot loader | `deploy/bootstrap.py` | at container start, downloads ONLY boot-critical objects (`webapp.db`, `reference/bonds_catalog.json`, CAS sample, identity.json). Everything else is lazy-fetched per request by `webapp/remote_store.py`. |
| Lazy fetch | `webapp/remote_store.py` | `ensure()` pulls any missing `data/<path>` from R2 on first read. Never raises — degrades to local behaviour when R2 env vars are absent. |
| Server | `webapp/main.py run()` | binds `0.0.0.0:$PORT` (Railway injects `PORT`). Healthcheck: `/api/health`. |

## Environment variables (Railway service)

```
R2_ACCOUNT_ID=…            # Cloudflare account id
R2_ACCESS_KEY_ID=…         # R2 API token
R2_SECRET_ACCESS_KEY=…
R2_BUCKET=mf-holdings
MF_READONLY_DB=1           # required for bootstrap to run
```

Local equivalents live in `deploy/.env` (gitignored) for the upload tooling.

## Redeploy options

1. **Code change → auto-deploy (default).**
   `git push origin main` → Railway rebuilds and restarts. The ephemeral disk
   resets, so bootstrap re-pulls the CURRENT R2 snapshot at boot.
2. **Manual redeploy (no code change).**
   Railway dashboard → Service → Deployments → *Redeploy*, or
   `railway redeploy` (CLI). Use when you only bumped env vars or want a
   clean disk that re-pulls the latest R2 data.
3. **Data-only refresh (no redeploy needed for the next boot).**
   ```
   python deploy/prepare_data.py     # re-stage latest data + manifest
   python deploy/upload_r2.py        # push changed objects only
   ```
   New/changed files are served to NEW instances immediately (bootstrap +
   lazy fetch). A long-running instance keeps its already-downloaded copies,
   so follow with option 2 if it must see the new data without a code push.

## Refresh checklist used on 2026-08-22

- `python deploy/prepare_data.py`  → staged 8,587 files / 826 MB
- `python deploy/upload_r2.py`     → 1 object changed (updated bond catalog), manifest updated
- commit + `git push origin main`  → Railway redeploys and boots on the fresh snapshot
