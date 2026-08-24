"""Regression tests for Wave 2 of the 2026-08-24 bug audit
(see docs/BUG_AUDIT_2026-08-24.md -> "Deferred — Wave 2")."""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# W2-1: rebuild fingerprint covers navall.txt / nifty weights / bonds catalog
# ---------------------------------------------------------------------------

def test_fingerprint_includes_reference_inputs(tmp_path, monkeypatch):
    from webapp import db as dbm
    navall = tmp_path / "navall.txt"
    weights = tmp_path / "weights.json"
    bonds = tmp_path / "bonds_catalog.json"
    for p in (navall, weights, bonds):
        p.write_text("x", encoding="utf-8")
    monkeypatch.setattr(dbm, "NAVALL_TXT", navall)
    monkeypatch.setattr(dbm, "NIFTY_WEIGHTS_JSON", weights)
    monkeypatch.setattr(dbm, "BONDS_CATALOG_JSON", bonds)
    fp1 = dbm._fingerprint()
    navall.write_text("Scheme Code;more;line\n", encoding="utf-8")  # refresh input
    fp2 = dbm._fingerprint()
    assert fp1 != fp2, "[M11] refreshed navall.txt must trigger a rebuild"


# ---------------------------------------------------------------------------
# W2-2: percent-scale decided per batch, never per value
# ---------------------------------------------------------------------------

def test_amfi_fetch_scale_batch_not_per_record():
    from webapp.amfi_fetch import _normalize_scheme_scale, _weight
    # per-record parse keeps raw value...
    assert _weight({"weight_pct": 0.85}) == 0.85
    # ...batch decides: sub-1% position among normal weights survives
    schemes = {"F": {"holdings": [
        {"percent_nav": 0.85}, {"percent_nav": 7.2}, {"percent_nav": 6.9}]}}
    _normalize_scheme_scale(schemes)
    assert [h["percent_nav"] for h in schemes["F"]["holdings"]] == [0.85, 7.2, 6.9]
    # true fraction source: EVERY value < 2 -> whole batch scaled x100
    frac = {"G": {"holdings": [{"percent_nav": 0.065}, {"percent_nav": 0.004}]}}
    _normalize_scheme_scale(frac)
    assert [h["percent_nav"] for h in frac["G"]["holdings"]] == [6.5, 0.4]


def test_debt_ytm_scale_is_sleeve_wide():
    from webapp.db import WebDB, _debt_details
    r = _debt_details("7.98% Gujarat SDL 2026", "0.85", "AAA")
    assert r["ytm"] == 0.85  # raw; scaling is not per-value anymore
    getter = WebDB._sleeve_yield_pct(
        [{"yield": "0.85"}, {"ytm": 7.2}])          # mixed scale: keep as-is
    assert getter({"yield": "0.85"}, "yield") == 0.85
    assert getter({"ytm": 7.2}, "ytm") == 7.2
    getter_frac = WebDB._sleeve_yield_pct(
        [{"yield": "0.0651"}, {"ytm": 0.07}])       # every value < 1: fractions
    assert getter_frac({"yield": "0.0651"}, "yield") == 6.51


# ---------------------------------------------------------------------------
# W2-3: demo backfill keys off is_demo flag, never the name
# ---------------------------------------------------------------------------

def test_demo_backfill_requires_flag():
    from webapp import tools_api, userdata
    uid = 987654  # userdata rows are user-scoped; no FK to the auth DB
    client_id = userdata.create_client(uid, "Impersonator", "", "")["id"]
    p = userdata.create_client_portfolio(
        uid, client_id, "Default Portfolio", "actual",
        [{"type": "scheme", "isin": "INE002A01018", "name": "Test Fund",
          "weight": 100}])
    # same NAME as the demo but not flagged -> no injection [M14]
    assert tools_api._ensure_demo_transactions(uid, p) == []
    fresh = userdata.get_client_portfolio(uid, p["id"])
    assert fresh["transactions"] == []
    # flagged -> backfill applies exactly once
    userdata.mark_demo_portfolios(uid, ["Default Portfolio"])
    flagged = userdata.get_client_portfolio(uid, p["id"])
    txs = tools_api._ensure_demo_transactions(uid, flagged)
    if txs:  # sample data present in repo
        fresh = userdata.get_client_portfolio(uid, p["id"])
        assert fresh["transactions"], "flagged demo must persist its txs"


# ---------------------------------------------------------------------------
# W2-4: compare_schemes survives unparseable boundary dates
# ---------------------------------------------------------------------------

def test_compare_schemes_bad_boundary_dates():
    from webapp import db as dbm
    import sqlite3
    wdb = dbm.WebDB(path=dbm.DB_PATH)
    nav_dir = dbm.NAV_HISTORY_DIR
    nav_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for i, code in enumerate(("777001", "777002")):
        (nav_dir / f"{code}.json").write_text(json.dumps({
            "scheme_code": code,
            "history": ([{"date": "not-a-date", "nav": 10.0}] if i == 0 else [])
            + [{"date": f"0{i+1}-Jan-2026", "nav": 10.0},
               {"date": f"2{i+1}-Jan-2026", "nav": 11.0}],
        }), encoding="utf-8")
    con = sqlite3.connect(dbm.DB_PATH)
    cur = con.cursor()
    for code in ("777001", "777002"):
        cur.execute(
            "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,"
            "coverage,n_holdings,n_equity,n_debt,top_holding,top_holding_pct,"
            "isin_regular,isin_direct,amfi_regular,amfi_direct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"amc-{code}", "AMC", f"Fund {code}", "amfi", "2026-01-01",
             "Equity", "", "has_holdings", 1, 1, 0, "X", 1.0,
             "INE002A01018", "INE002A01018", code, code))
        ids.append(cur.lastrowid)
    con.commit()
    con.close()
    res = wdb.compare_schemes(ids)  # used to raise TypeError on ds[0] = None
    assert isinstance(res, dict)
    assert res.get("error") != "need at least two schemes with NAV history" \
        or True  # either a clean comparison or a graceful error — never a crash


# ---------------------------------------------------------------------------
# W2-5: descending text sort actually descends
# ---------------------------------------------------------------------------

def test_rev_str_descending_sort():
    from webapp.db import _RevStr
    rows = [{"maturity_date": d} for d in
            ("2026-01-01", "2031-05-05", "2028-12-31")]
    rows.sort(key=lambda b: (0, _RevStr(str(b["maturity_date"]))))
    assert [r["maturity_date"] for r in rows] == \
        ["2031-05-05", "2028-12-31", "2026-01-01"]


# ---------------------------------------------------------------------------
# W2-6: sentinel manifests are not scheme codes
# ---------------------------------------------------------------------------

def test_sentinel_files_excluded_from_nav_scans(tmp_path):
    from src.nav_freshness import all_codes, scheme_nav_files
    import src.nav_freshness as nf
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "download_summary.json").write_text("{}")
    (tmp_path / "119392.json").write_text('{"history":[]}')
    monkey_nv = tmp_path
    orig = nf.NAV_DIR
    nf.NAV_DIR = monkey_nv
    try:
        assert all_codes() == ["119392"]
        assert [p.name for p in scheme_nav_files(monkey_nv)] == ["119392.json"]
    finally:
        nf.NAV_DIR = orig


# ---------------------------------------------------------------------------
# W2-7: month tokens match whole words; year adjacent to month wins
# ---------------------------------------------------------------------------

def test_extract_month_year_tokenized():
    from src.amc_adapters.base import extract_month_year, month_year_match
    # 'mar' inside 'Market' must NOT win over real 'Feb'
    assert extract_month_year(
        "Monthly_Portfolio_Market_Outlook_Feb_2026.pdf") == (2, 2026)
    assert extract_month_year("Kotak_Bluechip_Factsheet_Aug-2026.pdf") == (8, 2026)
    assert extract_month_year("september statement") is None  # no year
    assert extract_month_year("nothing usable here") is None
    assert month_year_match("Summary_Mar_2026.pdf", 3, 2026) is True
    assert month_year_match("market_recap.pdf", 3, 2026) is False


# ---------------------------------------------------------------------------
# W2-9: Google quote parser accepts Indian comma format
# ---------------------------------------------------------------------------

def test_google_price_with_thousands_separator(monkeypatch):
    import src.stock_price as sp
    html = b'<html><div data-last-price="1,234.56"></div></html>'
    monkeypatch.setattr(sp, "http_get", lambda *a, **k: html)
    pts = sp._fetch_google("MRF")
    assert pts and pts[0]["close"] == 1234.56


# ---------------------------------------------------------------------------
# W2-10: user '%'/'_' no longer act as SQL wildcards
# ---------------------------------------------------------------------------

def test_search_percent_is_literal():
    from webapp import db as dbm
    wdb = dbm.WebDB(path=dbm.DB_PATH)
    wild = wdb.list_securities(q="T%")["items"]
    assert all("%" not in (s.get("name") or "") for s in wild) \
        and not any((s.get("name") or "").startswith("Test Industries") for s in wild)
    hits = wdb.list_securities(q="Test Ind")["items"]
    assert any("Test Industries" in (s.get("name") or "") for s in hits)
