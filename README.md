# Factsheet Engine AI — MF Holdings Portal

Web presence of the MF portfolio-holdings research pipeline: a FastAPI app
(`webapp/`) serving schemes, holdings, stocks and the NSE bond market over a
prebuilt SQLite cache, plus a zero-cost Railway + Cloudflare R2 deployment
(kit in `deploy/`).

## Quick start (local)

```powershell
pip install -r requirements.txt
python -m webapp           # http://localhost:8000
```

## Deploy (zero cost: Railway Free + Cloudflare R2)

```powershell
python deploy/prepare_data.py          # stage runtime data + fresh webapp.db
python deploy/upload_r2.py --verify    # push to R2 (needs deploy/.env)
```

See `deploy/` for the bootstrap script and `railway.json` for service config.

## Layout

- `data/` — parsed holdings, NAV/stock history, webapp.db (gitignored)
- `webapp/` — FastAPI app, auth, lazy R2-backed storage reads
- `src/` — pipeline (fetch/parse AMC PDFs, stock agents, bonds)
- `deploy/` — R2 staging, upload, Railway bootstrap
