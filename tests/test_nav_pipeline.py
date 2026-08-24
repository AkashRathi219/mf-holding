"""[NAV-STUB] nav_history self-heal + no-stub pipeline rules.

Regression tests for the 2026-08-24 cold-start incident: a fresh container's
nav_daily seeded ~3,394 recent-window-only files that permanently shadowed
the full R2 histories (remote_store.ensure trusts existing files), so
schemes with years of data honestly reported "not enough NAV history".

Rules under test:
  1. db._load_nav_plan heals a thin (<30 pt) file once per code per process,
     keeping whichever copy has MORE points (never downgrading).
  2. src.nav_daily never seeds thin stubs for universe codes: missing files
     are filled with FULL histories (R2 first, capped mfapi mirror) or left
     absent — absence is honest and self-healing, a stub lies forever.
"""

from __future__ import annotations

import json
from datetime import date, timedelta as td
from pathlib import Path

import pytest

from webapp import db as wdb

STUB_D0 = date(2026, 8, 17)  # Mon-Fri week -> the 5-point prod stub shape


def _stub_doc(code: str) -> dict:
    return {"scheme_code": code, "fund_name": "Stub Fund", "source": "AMFI",
            "history": [{"date": (STUB_D0 + td(days=i)).strftime("%d-%b-%Y"),
                         "nav": 100.0 + i} for i in range(5)]}


def _full_doc(code: str, n: int = 2744) -> dict:
    d0 = date(2015, 7, 23)
    return {"scheme_code": code, "fund_name": "Stub Fund", "source": "AMFI",
            "history": [{"date": (d0 + td(days=i)).strftime("%d-%b-%Y"),
                         "nav": round(100 + i * 0.06, 4)} for i in range(n)]}


@pytest.fixture()
def heal_env(tmp_path, monkeypatch):
    """Sandbox nav dir + scheme row backed by a 5-point stub file."""
    from conftest import WEBAPP_DB
    import sqlite3

    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb, "_nav_heal_attempted", set())
    (hist / "700001.json").write_text(
        json.dumps(_stub_doc("700001")), encoding="utf-8")

    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,"
        "coverage,amfi_regular,amfi_direct) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("t-heal", "Test AMC", "Stub Fund", "amfi", "2026-08-21", "Equity",
         "", "has_holdings", "700001", "700001"))
    sid = cur.execute(
        "SELECT id FROM schemes WHERE key='t-heal'").fetchone()[0]
    con.commit()
    con.close()
    yield sid, hist
    con = sqlite3.connect(WEBAPP_DB)
    con.execute("DELETE FROM schemes WHERE id=?", (sid,))
    con.commit()
    con.close()


def test_heal_on_read_upgrades_stub_from_r2(heal_env, monkeypatch):
    """A stub shadowing a full R2 object is upgraded on first read."""
    sid, hist = heal_env
    better = _full_doc("700001")

    def fake_download(relpath, dest):
        assert relpath == "nav_history/700001.json"
        dest.write_text(json.dumps(better), encoding="utf-8")
        return dest

    monkeypatch.setattr(wdb.remote_store, "download_to", fake_download)
    nav = wdb.get_db().scheme_nav(sid)
    assert nav["direct"] is not None and nav["direct"]["points"] == 2744
    assert nav["regular"]["points"] == 2744
    # the better copy is persisted over the stub (heal is durable)
    on_disk = json.loads((hist / "700001.json").read_text(encoding="utf-8"))
    assert len(on_disk["history"]) == 2744


def test_heal_never_downgrades_local(heal_env, monkeypatch):
    """A remote copy with FEWER points must never replace the local file."""
    sid, hist = heal_env
    thinner = {"scheme_code": "700001", "fund_name": "Stub Fund",
               "history": _stub_doc("700001")["history"][:3]}

    def fake_download(relpath, dest):
        dest.write_text(json.dumps(thinner), encoding="utf-8")
        return dest

    monkeypatch.setattr(wdb.remote_store, "download_to", fake_download)
    from src import fetch_missing_nav as fmn
    monkeypatch.setattr(fmn, "_fetch", lambda code: None)

    nav = wdb.get_db().scheme_nav(sid)
    assert nav["direct"]["points"] == 5  # local kept
    on_disk = json.loads((hist / "700001.json").read_text(encoding="utf-8"))
    assert len(on_disk["history"]) == 5


def test_heal_attempted_once_per_process(heal_env, monkeypatch):
    """When no better copy exists, the (network) heal runs at most once."""
    sid, hist = heal_env
    calls = {"dl": 0, "mf": 0}

    def fake_download(relpath, dest):
        calls["dl"] += 1
        return None  # R2: nothing

    monkeypatch.setattr(wdb.remote_store, "download_to", fake_download)
    from src import fetch_missing_nav as fmn

    def fake_fetch(code):
        calls["mf"] += 1
        return None  # mirror: nothing

    monkeypatch.setattr(fmn, "_fetch", fake_fetch)
    wdb.get_db().scheme_nav(sid)  # regular + direct loads
    wdb.get_db().scheme_nav(sid)  # second request: guarded
    assert calls["dl"] == 1 and calls["mf"] == 1
    # the thin file survives (charts keep their 5 points, analytics stays null)
    assert (hist / "700001.json").exists()


# ---- nav_daily: never seed stubs -----------------------------------------------


def _amfi_window_text(codes=("700001", "999999")):
    d0 = STUB_D0
    lines = ["Scheme Code;ISIN Div Payout; ISIN Growth;Scheme Name;Net Asset Value;Date"]
    for i in range(5):
        d = (d0 + td(days=i)).strftime("%d-%b-%Y")
        if "700001" in codes:
            lines.append(f"700001;Stub Fund;Regular;Growth;ISIN001;ISIN001R;{100 + i};{d}")
        if "999999" in codes:
            lines.append(f"999999;Other Fund;Regular;Growth;ISIN002;ISIN002R;{50 + i};{d}")
    return "\n".join(lines)


def test_nav_daily_fills_missing_with_full_history_not_stub(tmp_path, monkeypatch):
    """A universe code with no file gets a FULL history (mfapi mirror here),
    never a 5-day stub; non-universe codes stay skipped."""
    from src import nav_daily
    from src import fetch_missing_nav as fmn

    out_dir = tmp_path / "nav"
    monkeypatch.setattr(nav_daily, "load_universe",
                        lambda: [{"amfi_code": "700001"}])
    monkeypatch.setattr(nav_daily, "_fetch_amfi",
                        lambda frm, tod: _amfi_window_text())
    mirror = {"meta": {"scheme_name": "Stub Fund"},
              "data": [{"date": (date(2015, 7, 23) + td(days=i)).strftime("%d-%m-%Y"),
                        "nav": str(round(100 + i * 0.06, 4))}
                       for i in range(2744)]}
    seen = []
    monkeypatch.setattr(fmn, "_fetch", lambda code: (seen.append(code), mirror)[1]
                        if code == "700001" else None)

    summary = nav_daily._update_latest_navs_impl(days=10, out_dir=out_dir)
    f = out_dir / "700001.json"
    assert f.exists(), "universe code must be filled"
    doc = json.loads(f.read_text(encoding="utf-8"))
    assert len(doc["history"]) >= 2744  # full history, not a 5-day stub
    assert doc["history"][0]["date"].endswith("2015")  # starts at inception
    assert not (out_dir / "999999.json").exists()  # non-universe: skipped
    assert summary["created"] == 1
    assert seen == ["700001"]


def test_nav_daily_mirror_cap_leaves_file_absent(tmp_path, monkeypatch):
    """With the mirror capped out, absence beats a stub: the file is simply
    not written (the read path will fetch the full R2 object later)."""
    from src import nav_daily
    from src import fetch_missing_nav as fmn

    out_dir = tmp_path / "nav"
    monkeypatch.setattr(nav_daily, "load_universe",
                        lambda: [{"amfi_code": "700001"}])
    monkeypatch.setattr(nav_daily, "_fetch_amfi",
                        lambda frm, tod: _amfi_window_text())
    monkeypatch.setattr(nav_daily, "MAX_FULL_HISTORY_FETCHES_PER_RUN", 0)
    calls = []
    monkeypatch.setattr(fmn, "_fetch", lambda code: calls.append(code) or None)

    summary = nav_daily._update_latest_navs_impl(days=10, out_dir=out_dir)
    assert not (out_dir / "700001.json").exists()
    assert calls == []  # cap honoured: no mirror traffic at all
    assert summary["skipped"] >= 1
    assert summary["created"] == 0


# ---- scheme code resolver (human-review matching) -------------------------------


def _write_navall(tmp_path: Path) -> Path:
    p = tmp_path / "navall.txt"
    p.write_text(
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
        "Scheme Name;Net Asset Value;Date\n"
        "\n"
        "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n"
        "\n"
        "Bandhan Mutual Fund\n"
        "107620;INE769A01026;INE769A01034;Bandhan Core Equity Fund-Direct "
        "Plan-Growth;100.5;21-Aug-2026\n"
        "107619;INE769A01018;;Bandhan Core Equity Fund-Regular Plan-Growth;"
        "98.5;21-Aug-2026\n"
        "\n"
        "HDFC Mutual Fund\n"
        "107525;INE540H01027;;HDFC Infrastructure Fund-IDCW Plan;42.0;"
        "21-Aug-2026\n",
        encoding="utf-8")
    return p


def test_resolver_directory_tracks_amc_headers(tmp_path):
    from scripts.resolve_scheme_codes import load_amfi_directory
    d = load_amfi_directory(_write_navall(tmp_path))
    assert d["bandhan core equity fund direct plan growth"] == ("107620",
                                                                "Bandhan Mutual Fund")
    assert d["hdfc infrastructure fund idcw plan"][1] == "HDFC Mutual Fund"


def test_resolver_exact_and_fuzzy_candidates(tmp_path):
    from scripts.resolve_scheme_codes import candidates, load_amfi_directory
    d = load_amfi_directory(_write_navall(tmp_path))
    # exact normalized match -> ratio 1.0
    got = candidates("Bandhan Core Equity Fund-Direct Plan-Growth", d)
    assert got[0][:2] == ("107620",
                          "bandhan core equity fund direct plan growth")
    assert got[0][2] == 1.0
    # renamed house (IDFC -> Bandhan): fuzzy match surfaces the right fund
    # WITH its (different) AMC so the human reviewer sees the trap
    got = candidates("IDFC Core Equity Fund", d)
    assert got and got[0][0] == "107620"
    assert "bandhan" in got[0][1]
    assert got[0][2] >= 0.55 and "Bandhan Mutual Fund" in got[0][3]


# ---- preheal + analytics cache --------------------------------------------------


def test_preheal_upgrades_thin_files(tmp_path, monkeypatch):
    """preheal_nav_stubs sweeps thin files and upgrades them via the heal."""
    from webapp.db import NAV_STUB_HEAL_MIN_POINTS, preheal_nav_stubs
    import webapp.db as dbmod

    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb, "_nav_heal_attempted", set())
    (hist / "700001.json").write_text(json.dumps(_stub_doc("700001")),
                                      encoding="utf-8")
    (hist / "700002.json").write_text(json.dumps(_full_doc("700002")),
                                      encoding="utf-8")  # healthy: untouched

    def fake_download(relpath, dest):
        dest.write_text(json.dumps(_full_doc(relpath.split("/")[-1])),
                        encoding="utf-8")
        return dest

    monkeypatch.setattr(wdb.remote_store, "download_to", fake_download)
    summary = preheal_nav_stubs(limit=100)
    assert summary["scanned_thin"] == 1
    assert summary["healed"] == 1
    on_disk = json.loads((hist / "700001.json").read_text(encoding="utf-8"))
    assert len(on_disk["history"]) == 2744
    # healthy file untouched
    assert len(json.loads((hist / "700002.json").read_text(encoding="utf-8"))
               ["history"]) == 2744


def test_preheal_respects_limit(tmp_path, monkeypatch):
    from webapp.db import preheal_nav_stubs
    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb, "_nav_heal_attempted", set())
    for code in ("700001", "700002", "700003"):
        (hist / f"{code}.json").write_text(json.dumps(_stub_doc(code)),
                                           encoding="utf-8")
    monkeypatch.setattr(wdb.remote_store, "download_to",
                        lambda relpath, dest: None)
    from src import fetch_missing_nav as fmn
    monkeypatch.setattr(fmn, "_fetch", lambda code: None)
    summary = preheal_nav_stubs(limit=2)
    assert summary["scanned_thin"] == 3
    assert summary["attempted"] == 2
    assert summary["deferred"] == 1


def test_analytics_cache_hits_on_same_data(tmp_path, monkeypatch):
    """Second identical call is served from the module-level cache."""
    from webapp import analytics as ana

    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb, "_nav_heal_attempted", set())
    monkeypatch.setattr(wdb, "_analytics_cache", {})
    (hist / "700001.json").write_text(json.dumps(_full_doc("700001", n=400)),
                                      encoding="utf-8")

    import sqlite3
    from conftest import WEBAPP_DB
    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,"
        "coverage,amfi_regular,amfi_direct) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("t-cache", "Test AMC", "Cache Fund", "amfi", "2026-08-21", "Equity",
         "", "has_holdings", "700001", "700001"))
    sid = cur.execute("SELECT id FROM schemes WHERE key='t-cache'").fetchone()[0]
    con.commit()
    con.close()

    calls = {"n": 0}
    real = ana.compute_series_analytics

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(ana, "compute_series_analytics", counting)
    a1 = wdb.get_db().scheme_analytics(sid)
    assert calls["n"] == 1
    a2 = wdb.get_db().scheme_analytics(sid)
    assert calls["n"] == 1  # cache hit — engine not re-run
    assert a2 == a1 and a2 is not a1  # equal content, defensive copy


def test_resolver_renamed_house_beats_wrong_house_lookalike(tmp_path):
    """IDFC -> Bandhan rename: 'IDFC Infrastructure Fund' must rank the
    BANDHAN Infrastructure Fund above the HDFC same-named lookalike, and the
    scheme's own (already-renamed) AMC boosts that ranking further."""
    from scripts.resolve_scheme_codes import candidates, load_amfi_directory
    p = tmp_path / "navall.txt"
    p.write_text(
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
        "Scheme Name;Net Asset Value;Date\n"
        "HDFC Mutual Fund\n"
        "107525;INE540H01027;;HDFC Infrastructure Fund-IDCW Plan;42.0;21-Aug-2026\n"
        "Bandhan Mutual Fund\n"
        "114476;INF194K01BY9;;BANDHAN Infrastructure Fund - Regular Plan - Growth;"
        "88.0;21-Aug-2026\n"
        "118469;INF194K01X46;;BANDHAN Infrastructure Fund-Direct Plan-Growth;"
        "90.0;21-Aug-2026\n",
        encoding="utf-8")
    d = load_amfi_directory(p)
    got = candidates("IDFC Infrastructure Fund", d,
                     scheme_amc="Bandhan Mutual Fund", scheme_plan="Regular")
    assert got, "no candidates"
    assert got[0][0] == "114476"  # the Regular-plan Bandhan successor, not
    # HDFC 107525 and not the Direct-plan sibling 118469
    assert "bandhan" in got[0][1] and "hdfc" not in got[0][1]
    assert "regular" in got[0][1]
    # plan alignment: a Direct-plan scheme flips to the Direct sibling
    got_d = candidates("IDFC Infrastructure Fund", d,
                       scheme_amc="Bandhan Mutual Fund", scheme_plan="Direct")
    assert got_d[0][0] == "118469"


def test_resolver_apply_writes_only_approved(tmp_path, monkeypatch):
    import sqlite3
    from conftest import WEBAPP_DB
    from scripts import resolve_scheme_codes as rsc
    from webapp.db import _create_schema

    review = tmp_path / "review.csv"
    review.write_text(
        "scheme_id,fund_name,plan,source,proposed_code,proposed_name,"
        "proposed_amc,ratio,approved\n"
        "1,Test Fund,Regular,amc_website,107619,bandhan core equity fund "
        "regular plan growth,Bandhan Mutual Fund,0.9,yes\n"
        "2,Other Fund,Regular,amc_website,107525,hdfc infrastructure fund "
        "idcw plan,HDFC Mutual Fund,0.8,\n",
        encoding="utf-8")
    db_path = tmp_path / "db.sqlite"
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    _create_schema(cur)
    cur.execute("INSERT INTO schemes (key,fund_name,plan) VALUES ('a','Test Fund','Regular')")
    cur.execute("INSERT INTO schemes (key,fund_name,plan) VALUES ('b','Other Fund','Regular')")
    con.commit()
    con.close()

    applied = rsc.apply_approved(db_path=db_path, review_csv=review,
                                 fetch_history=False)
    assert applied == 1  # only the approved=yes row
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    a = con.execute("SELECT amfi_regular FROM schemes WHERE key='a'").fetchone()
    b = con.execute("SELECT amfi_regular FROM schemes WHERE key='b'").fetchone()
    con.close()
    assert a["amfi_regular"] == "107619"
    assert b["amfi_regular"] in (None, "")
