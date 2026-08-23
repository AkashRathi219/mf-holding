"""ANA3: portfolio-level analytics over reconstructed weighted NAV series."""

from __future__ import annotations

import json
from datetime import date, timedelta as td

import pytest


def _nav_doc(code: str, name: str, growth: float, n_days: int = 420) -> dict:
    d0 = date(2025, 6, 1)
    return {"scheme_code": code, "fund_name": name, "category": "Equity",
            "plan": "Regular", "option": "Growth", "currency": "INR",
            "history": [{"date": (d0 + td(days=i)).strftime("%d-%b-%Y"),
                         "nav": round(100 * growth ** i, 4)}
                        for i in range(n_days)]}


@pytest.fixture()
def basket(tmp_path, monkeypatch):
    """Two equity schemes w/ NAV histories in the sandbox webapp.db."""
    from conftest import WEBAPP_DB
    import sqlite3

    from webapp import db as wdb

    hist_dir = tmp_path / "navhist"
    hist_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist_dir)

    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    ids = []
    for code, name, g in (("900101", "Alpha Fund Regular", 1.0006),
                          ("900102", "Beta Fund Regular", 1.0002)):
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


def test_weighted_reconstruction_blend(basket):
    """60/40 blend of two deterministic series -> portfolio CAGR sits strictly
    between the constituents' CAGRs."""
    from webapp import db as wdb
    a_id, b_id = basket
    out = wdb.get_db().portfolio_analytics([
        {"type": "scheme", "id": a_id, "weight": 60},
        {"type": "scheme", "id": b_id, "weight": 40}])
    assert not out.get("error"), out
    cons = {c["scheme_id"]: c for c in out["constituents"]}
    assert set(cons) == set(basket)
    assert abs(sum(c["weight"] for c in cons.values()) - 100.0) < 0.01
    g = out["growth_100"]
    assert g["values"][0] == 100.0
    # blend must beat the weaker leg's own growth at the same end date
    solo_b = wdb.get_db().portfolio_analytics(
        [{"type": "scheme", "id": b_id, "weight": 100}])
    assert g["values"][-1] > solo_b["growth_100"]["values"][-1]
    assert out["cagr_pct"]["since_inception"] > \
        solo_b["cagr_pct"]["since_inception"]
    assert out["disclaimer"].startswith("Past performance")


def test_weights_normalised_and_duplicates_deduped(basket):
    from webapp import db as wdb
    a_id, b_id = basket
    out = wdb.get_db().portfolio_analytics([
        {"type": "scheme", "id": a_id, "weight": 300},
        {"type": "scheme", "id": b_id, "weight": 100},
        {"type": "scheme", "id": a_id, "weight": 50},  # dup ignored
        {"type": "stock", "name": "X", "weight": 50},  # non-scheme skipped
        {"type": "scheme", "id": a_id, "weight": 0},   # zero-weight dropped
    ])
    assert not out.get("error")
    weights = [c["weight"] for c in out["constituents"]]
    assert len(weights) == 2
    assert abs(weights[0] - 75.0) < 0.01  # 300/400 normalised


def test_unresolvable_items_error_honestly(basket):
    from webapp import db as wdb
    out = wdb.get_db().portfolio_analytics([{"type": "stock", "name": "ZZZ"}])
    assert out.get("error") and not out.get("risk")
