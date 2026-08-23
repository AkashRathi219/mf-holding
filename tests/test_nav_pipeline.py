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
