"""Dashboard coverage check: boot the FastAPI app in-process (sandboxed
DBs, real data/ statement documents) and verify every stock with a parsed
statement document actually serves financial statements on the dashboard
endpoints the UI renders.

Run from the repo root:
    python scripts/dashboard_coverage_check.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- offline / sandbox environment (mirrors tests/conftest.py) ---------------
os.environ["SECRET_KEY"] = "coverage-check-secret-key"
os.environ["SUPERADMIN_EMAILS"] = "super@test.local"
os.environ.pop("ENABLE_SCHEDULER", None)
os.environ.pop("MF_READONLY_DB", None)
for _v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
           "R2_BUCKET", "R2_PREFIX"):
    os.environ.pop(_v, None)

_TMP = Path(tempfile.mkdtemp(prefix="mfh-coverage-"))
WEBAPP_DB = _TMP / "webapp.db"
AUTH_DB = _TMP / "auth.db"
USERDATA_DB = _TMP / "userdata.db"

import webapp.auth as auth  # noqa: E402
import webapp.db as db  # noqa: E402
import webapp.userdata as userdata  # noqa: E402

auth.AUTH_DB_PATH = AUTH_DB
userdata.USERDATA_DB = USERDATA_DB
db.DB_PATH = WEBAPP_DB
for _attr, _sub in (("NAV_HISTORY_DIR", "nav_history"),
                    ("STOCK_HISTORY_DIR", "stock_history"),
                    ("STOCK_ACTIONS_DIR", "stock_actions"),
                    ("STOCK_REPORTS_DIR", "stock_reports")):
    setattr(db, _attr, _TMP / _sub)
db.BONDS_CATALOG_JSON = _TMP / "reference" / "bonds_catalog.json"
(_TMP / "reference").mkdir(exist_ok=True)

con = sqlite3.connect(WEBAPP_DB)
cur = con.cursor()
db._create_schema(cur)

ident = json.loads((ROOT / "data" / "stocks" / "identity.json")
                   .read_text(encoding="utf-8"))
sym_by_isin = {i: (r.get("symbol") or "") for i, r in ident.items()}

docs = sorted(p.stem for p in
              (ROOT / "data" / "stock_financials").glob("*.json"))
for isin in docs:
    cur.execute(
        "INSERT INTO securities (isin,name,aliases,source_count,"
        "confirmed_equity,cap,sector) VALUES (?,?,?,?,?,?,?)",
        (isin, sym_by_isin.get(isin) or isin, "", 1, 1.0, "", ""))
con.commit()
con.close()

_sandbox = db.WebDB(path=WEBAPP_DB)
db.get_db = lambda force=False: _sandbox

from fastapi.testclient import TestClient  # noqa: E402
import webapp.main as main_mod  # noqa: E402

app = main_mod.app
client = TestClient(app)

r = client.post("/api/auth/register", json={
    "email": "coverage@test.local", "name": "Coverage Check",
    "org": "local", "password": "Coverage#2026"})
if r.status_code != 200:
    r = client.post("/api/auth/login", json={
        "email": "coverage@test.local", "password": "Coverage#2026"})
assert r.status_code == 200, r.text
H = {"Authorization": f"Bearer {r.json()['token']}"}

fin_ok = fin_bad = tbl_ok = tbl_bad = 0
bad_fin: list[str] = []
bad_tbl: list[str] = []
periods = {"FY": 0, "Q": 0, "?": 0}
q_cols = fy_cols = 0
for isin in docs:
    r1 = client.get(f"/api/securities/{isin}/financials", headers=H)
    d1 = r1.json() if r1.status_code == 200 else {}
    if r1.status_code == 200 and d1.get("available"):
        fin_ok += 1
    else:
        fin_bad += 1
        bad_fin.append(f"{isin}:{r1.status_code}")
    r2 = client.get(f"/api/securities/{isin}/financials/table", headers=H)
    d2 = r2.json() if r2.status_code == 200 else {}
    if r2.status_code == 200 and d2.get("available"):
        tbl_ok += 1
        p = d2.get("period") or "?"
        periods[p] = periods.get(p, 0) + 1
        if p == "Q":
            q_cols += len(d2.get("columns") or [])
        else:
            fy_cols += len(d2.get("columns") or [])
    else:
        tbl_bad += 1
        bad_tbl.append(f"{isin}:{r2.status_code}")

print(f"statement docs seeded as securities : {len(docs)}")
print(f"GET /financials        available    : {fin_ok}  unavailable: {fin_bad}")
print(f"GET /financials/table  available    : {tbl_ok}  unavailable: {tbl_bad}")
print(f"table basis: FY={periods.get('FY', 0)}  "
      f"Q(unaudited fallback)={periods.get('Q', 0)}")
print(f"columns rendered: FY-basis {fy_cols} annual cols · "
      f"Q-basis {q_cols} quarter cols")
if bad_fin:
    print("no /financials doc for:", ", ".join(bad_fin[:20]))
if bad_tbl:
    print("table unavailable for:", ", ".join(bad_tbl[:20]))

# spot-check one consolidated-FY doc and one quarterly-fallback doc
for isin in docs:
    d = client.get(f"/api/securities/{isin}/financials/table",
                   headers=H).json()
    if d.get("available") and d.get("period") == "FY":
        c = (d["columns"] or [{}])[-1]
        print(f"\nsample FY doc {isin} ({d.get('symbol')}): "
              f"{c.get('fy')} revenue={c['values'].get('revenue_from_operations')} "
              f"pat={c['values'].get('pat')}")
        break
for isin in docs:
    d = client.get(f"/api/securities/{isin}/financials/table",
                   headers=H).json()
    if d.get("available") and d.get("period") == "Q":
        c = (d["columns"] or [{}])[-1]
        print(f"sample Q doc  {isin} ({d.get('symbol')}): "
              f"{c.get('fy')} revenue={c['values'].get('revenue_from_operations')} "
              f"pat={c['values'].get('pat')}")
        break
