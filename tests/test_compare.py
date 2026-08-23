"""ANA2: /api/schemes/compare — common-window growth-of-R100 + rolling series."""

from __future__ import annotations

import json
from datetime import date, timedelta as td

import pytest
from fastapi.testclient import TestClient


def _nav_doc(code: str, name: str, growth: float, n_days: int = 420) -> dict:
    d0 = date(2025, 6, 1)
    return {"scheme_code": code, "fund_name": name, "category": "Equity",
            "plan": "Regular", "option": "Growth", "currency": "INR",
            "history": [{"date": (d0 + td(days=i)).strftime("%d-%b-%Y"),
                         "nav": round(100 * growth ** i, 4)}
                        for i in range(n_days)]}


@pytest.fixture()
def two_schemes(tmp_path, monkeypatch):
    """Two equity schemes w/ NAV histories in the sandbox: up 5bp/dn 1bp/day."""
    from conftest import WEBAPP_DB
    import sqlite3

    from webapp import db as wdb

    hist_dir = tmp_path / "navhist"
    hist_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist_dir)

    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    ids = []
    for code, name, g in (("900001", "Up Fund Regular", 1.0005),
                          ("900002", "Down Fund Regular", 0.9999)):
        (hist_dir / f"{code}.json").write_text(
            json.dumps(_nav_doc(code, name, g)), encoding="utf-8")
        cur.execute(
            "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,"
            "coverage,n_holdings,amfi_regular) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"t-{code}", "Test AMC", name.replace(" Regular", ""),
             "amfi", "2026-08-01", "Equity", "", "has_holdings", 3, code))
        ids.append(cur.execute(
            "SELECT id FROM schemes WHERE key=?", (f"t-{code}",)).fetchone()[0])
    con.commit()
    con.close()
    yield ids

    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    cur.executemany("DELETE FROM schemes WHERE id=?", [(i,) for i in ids])
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def client():
    from webapp.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    from conftest import ensure_token
    return ensure_token(client, {"name": "Cmp", "email": "cmp@test.local",
                                 "org": "", "password": "password123"})


def _clean(ids):
    from conftest import WEBAPP_DB
    import sqlite3
    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    cur.executemany("DELETE FROM schemes WHERE id=?", [(i,) for i in ids])
    con.commit()
    con.close()


def test_compare_engine_common_window_and_growth(two_schemes):
    from webapp import db as wdb
    a_id, b_id = two_schemes
    r = wdb.get_db().compare_schemes([a_id, b_id])
    assert not r.get("error"), r
    assert len(r["schemes"]) == 2
    assert r["window"]["common"] is True
    up = next(s for s in r["schemes"] if s["fund_name"] == "Up Fund")
    dn = next(s for s in r["schemes"] if s["fund_name"] == "Down Fund")
    assert up["growth_100"]["values"][0] == 100.0
    assert dn["growth_100"]["values"][0] == 100.0
    assert up["growth_100"]["values"][-1] > dn["growth_100"]["values"][-1]
    assert up["rolling_1y"]["dates"]
    assert up["metrics"]["cagr_pct"]["since_inception"] > \
        dn["metrics"]["cagr_pct"]["since_inception"]
    assert r["disclaimer"].startswith("Past performance")


def test_compare_requires_two_min(client, auth):
    assert client.post("/api/schemes/compare", headers=auth,
                       json={"scheme_ids": [1]}).status_code == 400
    assert client.post("/api/schemes/compare",
                       json={"scheme_ids": [1, 2]}).status_code == 401


def test_compare_caps_at_twelve(client, auth, two_schemes):
    a, b = two_schemes
    r = client.post("/api/schemes/compare", headers=auth,
                    json={"scheme_ids": [a, b] + list(range(3, 13))})
    assert r.status_code == 200
    assert r.json()["window"].get("common") is True
