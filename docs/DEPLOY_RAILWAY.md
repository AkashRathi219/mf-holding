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

## Activating CapSolver (captcha solving) — currently INACTIVE

The captcha solver is **disabled**: no key is configured. The key never lives in
`config/settings.yaml` (the previously committed value was removed; it is
considered burned — rotate at CapSolver when activating).

1. Create/copy a key at <https://dashboard.capsolver.com> (rotate if reusing the old one).
2. Railway service → **Variables** → add:
   ```
   CAPSOLVER_API_KEY=CAP-…
   ```
3. Redeploy (option 2 above). Verify: container logs show no
   "Captcha solver not configured" warning on Kotak adapter runs.
4. Deactivate by deleting the variable. `config/settings.yaml` needs no change —
   it only names the env var (`captcha.api_key_env`).

## Enabling the in-process scheduler

Set `ENABLE_SCHEDULER=1` on the Railway service **only together with** the slim
requirements that include `pyyaml`, `apscheduler`, `httpx` (they are, since
23-Aug-2026). Startup is guarded: an import failure now degrades to a logged
error instead of crash-looping the deployment [S2b]. Confirm health via
`GET /api/admin/refresh-summary` → `pipelines.scheduler.last_status == "alive"`
and `/api/health` → `checks.scheduler.ok`.

## Required variables — full checklist

| Variable | Required | Purpose |
|---|---|---|
| `MF_READONLY_DB=1` | yes | boot uses the prebuilt DB, never rebuilds |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET` | yes | data pulls (bootstrap + lazy fetch) |
| `SECRET_KEY` | **yes (prod)** | token signing; without it login/register return 503 by design |
| `ENABLE_SCHEDULER=1` | optional | in-process cron jobs |
| `CORS_ORIGINS` | optional | allowlist for the marketing site |
| `SUPERADMIN_EMAILS` | optional | defaults to akash@aracharatventures.com |

## Troubleshooting a failed deploy

Symptom → cause → fix (read the deploy log top-down; bootstrap prints a
`BOOTSTRAP FAILED` block when it can prove the app cannot serve):

| Log says | Meaning | Fix |
|---|---|---|
| `R2 env vars missing`-era behaviour replaced by `MISSING: R2_*` + exit 1 | readonly mode but R2 variables absent/typo'd | add the four R2 vars |
| `webapp.db  FAILED` + `BOOTSTRAP FAILED … exit 1` | credentials rejected or bucket/prefix wrong | verify the four R2 values against `deploy/.env`; re-run `python deploy/upload_r2.py --verify` locally |
| boots, then `/api/health` → 503 with `checks.db.error` | DB fetched but unreadable (partial upload?) | re-run `prepare_data.py && upload_r2.py --verify`, redeploy |
| boots green but login/register return 503 "SECRET_KEY is not configured" | prod guard [H4] refusing to auto-generate a session-killing secret | set `SECRET_KEY` (any long random string) in Variables |
| `/api/version` shows an old `commit` | stale deployment | Railway → Redeploy |

Verify what's actually running: `GET /api/version` (commit SHA),
`GET /api/health` (db/scheduler/r2 flags). CI runs a container-parity
boot check (`boot-slim` job) on every push so import/deps regressions are
caught before they land here.

## Public URL & custom domain (Hostinger)

Railway never exposes a service publicly until you generate a domain:
service → **Settings → Networking → Generate Domain** → port **8080**.
Copy the EXACT name shown (it includes a random suffix, e.g.
`mf-holding-production-a1b2c3.up.railway.app`) — guessing the name yields
DNS_PROBE_FINISHED_NXDOMAIN even though the app runs.

### Pointing fundpulse.aracharatventures.com (Hostinger + Google Workspace)

1. **Railway first:** Settings → Networking → **Custom Domain** → enter
   `fundpulse.aracharatventures.com`. Railway displays a **CNAME target**
   (`<something>.up.railway.app`). Keep this tab open.
2. **Hostinger:** hPanel → Domains → `aracharatventures.com` →
   **DNS / Nameservers** → add record:
   | Type | Name | Points to | TTL |
   |---|---|---|---|
   | CNAME | `fundpulse` | *(the Railway CNAME target from step 1)* | auto |
3. Back in Railway, click **Verify/Save**. TLS certificate issues
   automatically once DNS propagates (minutes to ~1 h).
4. **Do NOT touch** existing MX/SPF/DKIM records — Google Workspace mail is
   unaffected by adding one CNAME. Leave nameservers on Hostinger defaults
   unless you deliberately move them.
5. When it resolves: set this URL as the production reference everywhere
   (`SITE_URL`, website placeholders — see task WEB1), and redeploy if any
   env var references changed.

Troubleshooting: `dig fundpulse.aracharatventures.com CNAME` (or
nslookup) should return the Railway target; ERR_SSL before cert issuance is
normal for the first few minutes; if Railway says "domain already taken"
you may have added it under another project/environment.
