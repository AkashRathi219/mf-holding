"""NAV freshness audit [NAV-FRESH]: publication-calendar math, per-scheme
classification, honest skipped-split telemetry, and durable 24h counters.

The headline case: on Monday 24-Aug-2026 08:31 IST the freshest published NAV
is Friday 21-Aug — CORRECT behaviour, asserted here so nobody "fixes" it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from src.nav_freshness import (  # noqa: E402
    BUCKETS, classify, expected_latest_nav_date, is_publish_day,
    nse_holidays, prev_publish_day, publish_days_between,
)
from src.refresh_log import parse_ts  # noqa: E402

IST = timedelta(hours=5, minutes=30)


def _ist(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi) + IST  # naive IST -> +05:30 aware


# ---- publication calendar ----------------------------------------------------

def test_monday_morning_sees_friday_nav():
    # The exact production observation: Mon 24-Aug-2026 08:31 IST.
    assert expected_latest_nav_date(_ist(2026, 8, 24, 8, 31)) == date(2026, 8, 21)


def test_friday_evening_after_cutoff_sees_same_day():
    assert expected_latest_nav_date(_ist(2026, 8, 21, 23, 30)) == date(2026, 8, 21)


def test_friday_before_cutoff_sees_thursday():
    assert expected_latest_nav_date(_ist(2026, 8, 21, 8, 31)) == date(2026, 8, 20)


def test_saturday_and_sunday_see_friday():
    assert expected_latest_nav_date(_ist(2026, 8, 22, 12, 0)) == date(2026, 8, 21)
    assert expected_latest_nav_date(_ist(2026, 8, 23, 20, 0)) == date(2026, 8, 21)


def test_holiday_monday_skips_to_friday():
    # 9-Nov-2026 is in the Diwali-Balipratipada holiday set.
    assert date(2026, 11, 9) in nse_holidays(2026)
    assert not is_publish_day(date(2026, 11, 9))
    assert prev_publish_day(date(2026, 11, 9)) == date(2026, 11, 6)
    assert expected_latest_nav_date(_ist(2026, 11, 10, 8, 0)) == date(2026, 11, 6)


def test_publish_days_between_counts_only_publish_days():
    fri, mon = date(2026, 8, 21), date(2026, 8, 24)
    assert publish_days_between(fri, mon) == 1      # Monday only
    assert publish_days_between(date(2026, 8, 20), mon) == 2  # Fri + Mon
    assert publish_days_between(mon, mon) == 0


# ---- classification ----------------------------------------------------------

def test_classify_buckets():
    now = _ist(2026, 8, 24, 8, 31)
    assert classify("21-Aug-2026", now=now)["bucket"] == "current"
    assert classify("20-Aug-2026", now=now)["bucket"] == "lag1"
    assert classify("14-Aug-2026", now=now)["bucket"] == "stale_recent"
    assert classify("01-Jul-2026", now=now)["bucket"] == "stale_deep"
    assert classify("01-Jan-2026", now=now)["bucket"] == "dead_suspect"
    assert classify(None, now=now)["bucket"] == "no_history"
    assert classify("garbage", now=now)["bucket"] == "no_history"


def test_classify_carries_expected_and_gap():
    now = _ist(2026, 8, 24, 8, 31)
    out = classify("20-Aug-2026", now=now)
    assert out["expected"] == "2026-08-21"
    assert out["publish_gap"] == 1


def test_bucket_vocabulary_complete():
    assert set(BUCKETS) == {"current", "lag1", "stale_recent", "stale_deep",
                            "dead_suspect", "no_history"}


# ---- nav_daily skipped split + expected_latest --------------------------------

def test_nav_daily_splits_skipped_and_reports_expected(tmp_path, monkeypatch):
    import src.nav_daily as nd

    rows = [
        ("111111", "21-Aug-2026", 10.0, "A", "Direct", "Growth", "INEAA", ""),
        ("222222", "21-Aug-2026", 11.0, "B", "Direct", "Growth", "INEBB", ""),
        ("333333", "21-Aug-2026", 12.0, "C", "Direct", "Growth", "INECC", ""),
    ]
    monkeypatch.setattr(nd, "_fetch_amfi", lambda s, e: "x")
    monkeypatch.setattr(nd, "_parse_nav_text", lambda t: rows)
    monkeypatch.setattr(nd, "load_universe",
                        lambda: [{"amfi_code": "111111"}, {"amfi_code": "333333"}])
    # existing file for 111111 already containing the latest date -> unchanged
    (tmp_path / "111111.json").write_text(json.dumps(
        {"scheme_code": "111111", "history": [{"date": "21-Aug-2026", "nav": 10.0}]}),
        encoding="utf-8")
    monkeypatch.setattr(nd, "OUT_DIR", tmp_path)
    # 333333 is in-universe with no file: seeding must fail both ways
    monkeypatch.setattr("webapp.remote_store.ensure", lambda p: None,
                        raising=False)
    # [DATA-POLICY] the batch fill walks the AMFI portal — mock it offline
    monkeypatch.setattr("src.nav_history.fetch_codes_history",
                        lambda codes, **kw: {"written": 0})

    summary = nd._update_latest_navs_impl(days=10, out_dir=tmp_path)
    assert summary["unchanged"] == 1
    assert summary["skipped"] == 2
    assert summary["skipped_nonuniverse"] == 1          # 222222 not tracked
    assert summary["skipped_unfilled"] == 1             # 333333 unfilled
    assert summary["expected_latest"]                   # calendar date present


# ---- durable 24h counters ------------------------------------------------------

def test_refresh_log_ok24h_survives_log_loss(tmp_path, monkeypatch):
    import src.refresh_log as rl
    log, state = tmp_path / "refresh_log.jsonl", tmp_path / "refresh_state.json"
    monkeypatch.setattr(rl, "LOG_PATH", log)
    monkeypatch.setattr(rl, "STATE_PATH", state)
    monkeypatch.setattr(rl, "_state_cache", None)
    monkeypatch.setattr(rl, "_push_state", lambda: None)

    ts = "unused"  # record() stamps its own IST timestamps
    rl.record("nav_daily", "success", window="w")
    assert state.exists(), "state file must be written"

    # Simulate a container redeploy: JSONL gone, state restored from R2.
    log.unlink()
    rl._state_cache = None
    s = rl.summary()
    p = s["pipelines"]["nav_daily"]
    assert p["ok_24h"] >= 1, "durable counter must survive log loss"
    assert p["last_status"] == "success"


def test_refresh_log_prunes_stale_counters(tmp_path, monkeypatch):
    import src.refresh_log as rl
    old = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    p = {"recent_ok": [old, fresh], "recent_err": [old]}
    rl._prune_recent(p)
    assert p["recent_ok"] == [fresh]
    assert p["recent_err"] == []


# ---- contract: endpoint, panel, schedule ---------------------------------------

def test_admin_endpoint_and_panel_contract():
    main_py = (ROOT / "webapp" / "main.py").read_text(encoding="utf-8")
    assert '"/api/admin/nav-freshness"' in main_py
    assert "run_audit(" in main_py
    app_js = (ROOT / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    for marker in ("nav-freshness?live=", "refreshFreshnessData",
                   "NF_BUCKET_META", "dead_suspect"):
        assert marker in app_js, f"admin panel missing {marker}"


def test_settings_evening_run_after_amfi_cutoff():
    import re
    txt = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    m = re.search(r"nav_refresh:\s*\n\s*enabled:\s*true\s*\n\s*hour:\s*(\d+)",
                  txt)
    assert m, "nav_refresh hour not found in settings.yaml"
    assert int(m.group(1)) >= 23, (
        "evening NAV run must fire at/after AMFI's ~23:00 IST publication")


def test_parse_ts_handles_ist_entries():
    assert parse_ts("2026-08-24T08:31:53+05:30") is not None
    assert parse_ts("2026-08-24T08:31:53") is not None   # legacy naive
    assert parse_ts("bogus") is None


# ---- endpoint smoke (superadmin) ---------------------------------------------

SUPERADMIN = {"name": "Test Super", "email": "super@test.local",
              "org": "t", "password": "password123"}


def test_nav_freshness_endpoint_authed():
    from fastapi.testclient import TestClient
    from webapp.main import app
    with TestClient(app) as c:
        from conftest import ensure_token
        headers = ensure_token(c, SUPERADMIN)
        r = c.get("/api/admin/nav-freshness?live=0&sample=0", headers=headers)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        for key in ("expected_latest", "files_scanned", "buckets",
                    "healthy_pct", "offenders", "offender_count"):
            assert key in body, f"audit payload missing {key}"
        assert set(body["buckets"]) == set(BUCKETS)
        # anonymous access must be rejected, not leak the audit
        anon = c.get("/api/admin/nav-freshness")
        assert anon.status_code in (401, 403)
