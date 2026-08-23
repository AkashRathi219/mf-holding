"""Phase 8 debt fixes: _norm_code leading zeros [DBT3], revision-aware NAV
upsert [DBT2], userdata cascades + orphan purge + indexes [DBT1/DBT6]."""

from __future__ import annotations

import sqlite3

import pytest


# ---- DBT3: code normalisation ------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("154477.0", "154477"),      # float artifact from Excel/CSV
    (" 154477 ", "154477"),
    ("012345", "012345"),        # leading zero MUST survive [DBT3]
    ("012345.0", "12345"),       # float artifact wins over phantom zeros
    ("", ""),
    (None, ""),
    ("abc", "abc"),
])
def test_norm_code_preserves_leading_zeros(raw, expected):
    from src.nav_history import _norm_code
    assert _norm_code(raw) == expected


# ---- DBT2: revision-aware NAV upsert ----------------------------------------

@pytest.fixture()
def nav_db(tmp_path, monkeypatch):
    import src.nav_history as nh
    monkeypatch.setattr(nh, "NAV_DB", tmp_path / "nav.db")
    con = sqlite3.connect(nh.NAV_DB)
    nh._init_db(con.cursor())
    con.commit()
    return nh, con


def test_nav_revision_replaces_old_value(nav_db):
    """AMFI republishes a corrected NAV -> stored value must update [DBT2]."""
    nh, con = nav_db
    cur = con.cursor()
    row = ("123456", "01-Aug-2026", 10.0, "Fund A", "Direct", "Growth",
           "INE000A01018", "")
    cur.execute(
        "INSERT INTO nav_history "
        "(scheme_code,date,nav,name,plan,option,isin,isin_re) "
        "VALUES (?,?,?,?,?,?,?,?)", row)
    con.commit()

    revised = [("123456", "01-Aug-2026", 10.55, "Fund A", "Direct", "Growth",
                "INE000A01018", "")]
    cur.executemany(
        """
        INSERT INTO nav_history
            (scheme_code,date,nav,name,plan,option,isin,isin_re)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(scheme_code,date) DO UPDATE SET
            nav=excluded.nav, name=excluded.name,
            plan=excluded.plan, option=excluded.option,
            isin=excluded.isin, isin_re=excluded.isin_re
        WHERE nav_history.nav IS NOT excluded.nav
            OR nav_history.name IS NOT excluded.name
        """, revised)
    con.commit()

    got = cur.execute(
        "SELECT nav FROM nav_history WHERE scheme_code='123456'").fetchone()
    assert got[0] == 10.55, "revised NAV was ignored by the upsert"
    n = cur.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
    assert n == 1, "upsert must not duplicate rows"


# ---- DBT1 / DBT6: userdata cascades, orphan purge, indexes --------------------

@pytest.fixture()
def ud(tmp_path, monkeypatch):
    import webapp.userdata as u
    monkeypatch.setattr(u, "USERDATA_DB", tmp_path / "userdata.db")
    return u


def test_delete_strategy_cascades(ud):
    s = ud.create_strategy(1, "S", "", "")
    ud.set_rules(1, s["id"], [{"rule_type": "max_weight", "field": "",
                               "operator": "<=", "value": 10}])
    rid = ud.save_analysis_run(1, 77, "client", s["id"], {"r": 1})
    ud.delete_strategy(1, s["id"])
    assert ud.get_rules(s["id"], user_id=1) == []
    runs = ud.list_analysis_runs(1)
    assert all(r["id"] != rid for r in runs), "run referencing strategy survived"
    assert ud.list_strategies(1) == []


def test_delete_client_portfolio_cascades_runs(ud):
    cp = ud.create_client_portfolio(1, 5, "P", "model", [])
    rid = ud.save_analysis_run(1, cp["id"], "client", None, {"r": 1})
    ud.delete_client_portfolio(1, cp["id"])
    assert all(r["id"] != rid for r in ud.list_analysis_runs(1))
    assert ud.get_client_portfolio(1, cp["id"]) is None


def test_delete_model_detaches_portfolios(ud):
    m = ud.create_model(1, "M", "", None, [{"type": "scheme", "id": 1}])
    cp = ud.create_client_portfolio(1, 5, "P", "model", [],
                                    model_portfolio_id=m["id"])
    ud.delete_model(1, m["id"])
    got = ud.get_client_portfolio(1, cp["id"])
    assert got is not None and got["model_portfolio_id"] is None, \
        "portfolio must be detached, not deleted with the model"


def test_orphan_purge_heals_legacy_rows(ud):
    """analysis_runs pointing at long-deleted portfolios are purged once on
    the schema upgrade [DBT1]."""
    rid = ud.save_analysis_run(1, 424242, "client", None, {"r": 1})
    con = sqlite3.connect(ud.USERDATA_DB)
    con.execute("PRAGMA user_version=1")  # simulate pre-upgrade DB
    con.commit()
    con.close()

    fresh = ud._conn()  # triggers _upgrade_once
    try:
        left = fresh.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE id=?", (rid,)).fetchone()[0]
        ver = fresh.execute("PRAGMA user_version").fetchone()[0]
    finally:
        fresh.close()
    assert left == 0 and ver >= ud._SCHEMA_VERSION


def test_user_id_indexes_exist(ud):
    con = ud._conn()
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    finally:
        con.close()
    for expected in ("idx_strategies_user", "idx_rules_strategy",
                     "idx_models_user", "idx_clients_user",
                     "idx_cportfolios_user", "idx_runs_user"):
        assert expected in names, f"missing index {expected}"


def test_indexes_actually_used(ud):
    ud.create_strategy(9, "x", "", "")
    con = ud._conn()
    try:
        plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM strategies WHERE user_id=9"
        ).fetchall()
    finally:
        con.close()
    assert any("idx_strategies_user" in str(row[-1]) for row in plan), plan
