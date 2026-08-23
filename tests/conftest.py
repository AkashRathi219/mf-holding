"""Shared fixtures: synthetic SQLite DBs + offline env for the FastAPI app.

Runs the whole suite against a temp sandbox so tests never touch (or need)
the real data/ tree, R2, or the network. Top-level setup executes before any
test module imports webapp.main, so env-dependent module constants are stable.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

# ---- offline / deterministic environment -----------------------------------
os.environ["SECRET_KEY"] = "test-secret-key-do-not-use-in-prod"
os.environ["SUPERADMIN_EMAILS"] = "super@test.local"
os.environ.pop("ENABLE_SCHEDULER", None)
os.environ.pop("MF_READONLY_DB", None)
for _v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
           "R2_BUCKET", "R2_PREFIX"):
    os.environ.pop(_v, None)

_TMP = Path(tempfile.mkdtemp(prefix="mfh-tests-"))
WEBAPP_DB = _TMP / "webapp.db"
AUTH_DB = _TMP / "webapp_auth.db"
USERDATA_DB = _TMP / "userdata.db"

for _sub in ("nav_history", "stock_history", "stock_actions", "stock_reports"):
    (_TMP / _sub).mkdir(parents=True, exist_ok=True)


def _make_webapp_db(path: Path) -> None:
    from webapp.db import _create_schema

    con = sqlite3.connect(path)
    cur = con.cursor()
    _create_schema(cur)
    cur.execute(
        "INSERT INTO securities (isin,name,aliases,source_count,confirmed_equity,cap,sector) "
        "VALUES (?,?,?,?,?,?,?)",
        ("INE002A01018", "Test Industries Ltd", "", 2, 1.0, "large", "Technology"))
    cur.execute(
        "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,coverage,"
        "n_holdings,n_equity,n_debt,top_holding,top_holding_pct,isin_regular,isin_direct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("test-amc-test-fund", "Test AMC", "Test Fund", "amfi", "2026-08-01",
         "Equity", "", "has_holdings", 2, 2, 0, "Test Industries Ltd", 4.2,
         "INE002A01018", "INE002A01018"))
    cur.execute(
        "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,coverage) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("test-amc-index-fund", "Test AMC", "Test Index Fund", "index", "",
         "Equity", "", "index_only"))
    cur.execute(
        "INSERT INTO holdings (scheme_id,amc,fund_name,company,isin,percent_nav,"
        "sector,section,asset_class,source,as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Test AMC", "Test Fund", "Test Industries Ltd", "INE002A01018",
         4.2, "Technology", "Equity", "equity", "amfi", "2026-08-01"))
    con.commit()
    con.close()


_make_webapp_db(WEBAPP_DB)

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


def _sandbox_get_db(force: bool = False):
    return db.WebDB(path=WEBAPP_DB)


db.get_db = _sandbox_get_db


def ensure_token(client, creds: dict) -> dict:
    """Register-or-login; the auth DB persists across test modules."""
    r = client.post("/api/auth/register", json=creds)
    if r.status_code != 200:
        r = client.post("/api/auth/login", json={
            "email": creds["email"], "password": creds["password"]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def sandbox_dir() -> Path:
    return _TMP
