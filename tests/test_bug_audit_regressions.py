"""Regression tests for the 2026-08-24 bug audit (docs/BUG_AUDIT_2026-08-24.md).

Each test pins a bug that shipped green through the existing suite.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from webapp.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_auth_limiters():
    """Auth limiters are process-global and keyed by peer IP (identical for
    TestClient); reset around each test so registrations don't trip 429s."""
    from webapp.ratelimit import AUTH_LOGIN_LIMITER, AUTH_REGISTER_LIMITER
    AUTH_LOGIN_LIMITER.reset()
    AUTH_REGISTER_LIMITER.reset()
    yield
    AUTH_LOGIN_LIMITER.reset()
    AUTH_REGISTER_LIMITER.reset()


@pytest.fixture()
def user_client(client, tmp_path, monkeypatch):
    """One registered user -> {client, headers, uid}, with upload/pricing
    side-paths sandboxed to tmp so tests never touch data/."""
    from conftest import ensure_token
    import webapp.market_value as mv
    import webapp.db as dbm
    creds = {"email": "audit@test.local", "password": "password1",
             "name": "Audit"}
    hdr = ensure_token(client, creds)
    r = client.post("/api/auth/login", json={"email": creds["email"],
                                             "password": creds["password"]})
    uid = r.json()["user"]["id"]
    # hermetic pricing + ingest paths
    monkeypatch.setattr(mv, "NAV_DIR", tmp_path / "nav_history")
    monkeypatch.setattr(mv, "STOCK_DIR", tmp_path / "stock_history")
    monkeypatch.setattr(mv, "INDEX_PATH", tmp_path / "isin_latest_nav.json")
    monkeypatch.setattr(dbm, "DATA_DIR", tmp_path)
    return {"client": client, "headers": hdr, "uid": uid}


# ---------------------------------------------------------------------------
# BUG-C1: partial-update PUTs must never write NULL over stored values
# ---------------------------------------------------------------------------

def test_update_model_name_only_keeps_items(user_client):
    from webapp import userdata
    uid = user_client["uid"]
    m = userdata.create_model(uid, "Model A", "desc", None,
                              [{"type": "scheme", "isin": "INE002A01018",
                                "name": "Test Fund", "weight": 100}],
                              allocations={"eq": 100})
    updated = userdata.update_model(uid, m["id"], name="Model A renamed")
    assert updated["name"] == "Model A renamed"
    # [C1] rename must not wipe description/items/allocations/strategy link
    assert updated["description"] == "desc"
    assert updated["items"] == m["items"]
    assert updated["allocations"] == {"eq": 100}


def test_update_client_portfolio_rename_keeps_holdings_and_txs(user_client):
    from webapp import userdata
    uid = user_client["uid"]
    client_row = userdata.create_client(uid, "Client X", "", "")
    p = userdata.create_client_portfolio(uid, client_row["id"], "Port", "actual",
                                         [{"type": "scheme", "isin": "INE002A01018",
                                           "name": "Test Fund", "weight": 50}])
    tx = [{"date": "2026-01-02", "type": "PURCHASE", "amount": 5000.0}]
    userdata.update_client_portfolio(uid, p["id"], transactions=tx)
    renamed = userdata.update_client_portfolio(uid, p["id"], name="New name")
    assert renamed["name"] == "New name"
    assert renamed["items"], "[C1] items wiped by rename"
    assert renamed["transactions"] == tx, "[C1] transactions wiped by rename"
    assert renamed["kind"] == "actual"


def test_strategy_rules_only_update_keeps_name(user_client):
    from webapp import userdata
    uid = user_client["uid"]
    s = userdata.create_strategy(uid, "Strat", "", "Max 10% single stock")
    out = userdata.update_strategy(uid, s["id"], rules_text="Min 30% debt")
    assert out["name"] == "Strat", "[C1] NOT NULL name nulled by rules-only edit"
    assert out["rules_text"] == "Min 30% debt"


# ---------------------------------------------------------------------------
# BUG-C4: re-uploading holdings without transactions preserves cash-flow history
# ---------------------------------------------------------------------------

_CAS_TX = [{
    "date": "2026-01-02", "transaction_type": "PURCHASE", "units": "100",
    "amount": "10000", "isin": "INF000000101", "amfi_code": "700001",
    "scheme_name": "Fund One"}]


def _upload(client, headers, client_id, payload: dict, suffix=".json"):
    return client.post(f"/api/clients/{client_id}/documents",
                       files={"file": (f"doc{suffix}",
                                       json.dumps(payload).encode(),
                                       "application/json")},
                       headers=headers)


def test_document_reupload_keeps_transactions(user_client):
    from webapp import userdata
    hdr = user_client["headers"]
    cid = userdata.create_client(user_client["uid"], "C4 Client", "", "")["id"]

    doc = {"portfolio_summary": {"allocations": [
        {"scheme_name": "Fund One", "isin": "INF000000101", "net_units": 60}]},
        "transactions": _CAS_TX}
    r = _upload(user_client["client"], hdr, cid, doc)
    assert r.status_code == 200, r.text
    assert r.json()["parsed"] == 1

    # re-upload corrected holdings WITHOUT a transactions array (PDF-style)
    doc2 = {"portfolio_summary": {"allocations": [
        {"scheme_name": "Fund One", "isin": "INF000000101", "net_units": 55}]}}
    r = _upload(user_client["client"], hdr, cid, doc2)
    assert r.status_code == 200, r.text

    ports = userdata.list_client_portfolios(user_client["uid"])
    mine = [p for p in ports if p["client_id"] == cid]
    assert mine and mine[0]["transactions"], \
        "[C4] stored CAS transactions erased by holdings-only re-upload"


# ---------------------------------------------------------------------------
# BUG-C5: gold classification regex must actually match fund names
# ---------------------------------------------------------------------------

def test_gold_regex_matches_fund_names():
    import inspect
    from webapp import db
    src = inspect.getsource(db.WebDB.portfolio_analysis)
    m = re.search(r'gold_re = re\.compile\(\s*(r"[^"]*")\s*,\s*re\.I\s*\)', src)
    assert m, "gold_re definition not found"
    pattern = re.compile(eval(m.group(1)), re.I)  # the exact literal shipped
    assert pattern.search("Gold BeES ETF")
    assert pattern.search("GOLD Savings Fund")
    assert not pattern.search("Marigold Industries")  # \b boundary intact


# ---------------------------------------------------------------------------
# BUG-C3 / fetch_missing_nav: chronological sort keys
# ---------------------------------------------------------------------------

def test_date_key_is_chronological():
    from src.fetch_missing_nav import _date_key
    assert _date_key("18-Aug-2026") == "2026-08-18"
    assert _date_key("02-Jan-2026") == "2026-01-02"
    rows = ["18-Aug-2026", "31-Dec-2025", "02-Jan-2026", "15-Feb-2026"]
    assert sorted(rows, key=_date_key) == [
        "31-Dec-2025", "02-Jan-2026", "15-Feb-2026", "18-Aug-2026"]


def test_latest_nav_reader_picks_max_dated_row_not_last(tmp_path, monkeypatch):
    import webapp.market_value as mv
    nav_dir = tmp_path / "data" / "nav_history"
    nav_dir.mkdir(parents=True)
    misordered = {  # physically-last row is NOT the latest (BUG-C3 writer)
        "scheme_code": "700001", "history": [
            {"date": "05-Oct-2026", "nav": 21.0},
            {"date": "18-Aug-2026", "nav": 19.0},
            {"date": "02-Jan-2026", "nav": 11.0},
        ]}
    (nav_dir / "700001.json").write_text(json.dumps(misordered), encoding="utf-8")
    monkeypatch.setattr(mv, "BASE_DIR", tmp_path)
    scheme = {"amfi_regular": "700001", "amfi_direct": ""}
    got = mv.scheme_latest_nav(scheme)
    assert got == (21.0, "05-Oct-2026")


# ---------------------------------------------------------------------------
# BUG-H1/H2: proxy-aware rate limiting + fail-closed superadmin
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, xff: str = "", peer="203.0.113.9"):
        self.headers = {"X-Forwarded-For": xff} if xff else {}
        self.client = type("Peer", (), {"host": peer})()


def test_rate_limit_ip_ignores_xff_by_default(monkeypatch):
    from webapp import ratelimit
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    req = _FakeRequest(xff="1.2.3.4")
    assert ratelimit.client_ip(req) == "203.0.113.9"


def test_rate_limit_ip_trusted_hop_takes_rightmost(monkeypatch):
    from webapp import ratelimit
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    # attacker-chosen leftmost must be ignored behind a trusted appending proxy
    assert ratelimit.client_ip(_FakeRequest(xff="1.2.3.4, 198.51.100.7")) \
        == "198.51.100.7"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert ratelimit.client_ip(_FakeRequest(xff="1.2.3.4, 198.51.100.7")) \
        == "1.2.3.4"


def test_limiter_prunes_on_check_not_only_on_reject():
    from webapp.ratelimit import SlidingWindowRateLimiter
    lim = SlidingWindowRateLimiter(max_events=1000, window_seconds=0.0)
    lim.PRUNE_ABOVE = 1                     # shrink the soft cap for the test
    lim._hits["ghost"].append(0.0)          # long-expired entries
    lim._hits["ghost2"].append(0.0)
    lim.check("live")                        # no rejection happens here
    assert "ghost" not in lim._hits and "ghost2" not in lim._hits


def test_no_default_superadmin_email(monkeypatch):
    from webapp import auth
    monkeypatch.delenv("SUPERADMIN_EMAILS", raising=False)
    assert auth._superadmin_emails() == set()
    assert auth.superadmin_configured() is False
    monkeypatch.setenv("SUPERADMIN_EMAILS", " Boss@Example.com , ,ops@x.io ")
    assert auth._superadmin_emails() == {"boss@example.com", "ops@x.io"}


# ---------------------------------------------------------------------------
# BUG-M3/M4: strict id coercion + tolerant CAS parsing
# ---------------------------------------------------------------------------

def test_to_int_rejects_bad_input():
    from webapp.tools_api import _to_int
    with pytest.raises(HTTPException) as ei:
        _to_int("abc")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException):
        _to_int(4.5)                       # floats must not truncate silently
    with pytest.raises(HTTPException):
        _to_int(True)
    assert _to_int(" 7 ") == 7


def test_parse_cas_transactions_nav_na_does_not_crash():
    from webapp.tools_api import parse_cas_transactions
    doc = {"transactions": [
        {"date": "2026-01-05", "transaction_type": "REDEEM", "units": "10",
         "amount": "1230.00", "nav": "N/A", "isin": "INF000000101",
         "amfi_code": "700001", "scheme_name": "F"},
        "garbage-row",
        {"date": "2026-01-06", "transaction_type": "MYSTERY", "units": "1",
         "amount": "100"}]}
    out = parse_cas_transactions(doc)
    assert len(out) == 1
    assert out[0]["sign"] == -1.0
    assert out[0]["amount"] == -1230.0   # signed by type [perf-v1.4 contract]
    assert out[0]["nav"] is None         # was an unhandled ValueError before


# ---------------------------------------------------------------------------
# BUG-H5: corporate actions never shrink curated history on source failure
# ---------------------------------------------------------------------------

def test_refresh_actions_keeps_previous_when_source_fails(tmp_path, monkeypatch):
    import src.stock_actions as sa
    actions_dir = tmp_path / "stock_actions"
    actions_dir.mkdir()
    prev = {"isin": "INE002A01018", "symbol": "TESTIND", "dividends":
            [{"date": "01-Jan-2026", "amount": 2.5}],
            "splits": [{"date": "01-Jan-2025", "ratio": "1:2"}],
            "announcements": [{"date": "02-Feb-2026", "headline": "results"}]}
    (actions_dir / "INE002A01018.json").write_text(json.dumps(prev), encoding="utf-8")

    monkeypatch.setattr(sa, "ACTIONS_DIR", actions_dir)
    monkeypatch.setattr(sa, "_fetch_yahoo_events", lambda sym: ([], []))

    res = sa.refresh_actions("INE002A01018", {"symbol": "TESTIND", "name": "T"})
    assert res["status"] == "kept_previous"
    now = json.loads((actions_dir / "INE002A01018.json").read_text(encoding="utf-8"))
    assert now["dividends"] == prev["dividends"]
    assert now["splits"] == prev["splits"]
    assert now["announcements"] == prev["announcements"]


# ---------------------------------------------------------------------------
# BUG-M8/M10: scheduler hardening + atomic writes
# ---------------------------------------------------------------------------

def test_scheduler_job_defaults_present():
    from src.scheduler import _JOB_DEFAULTS, ist_now
    assert _JOB_DEFAULTS["misfire_grace_time"] >= 3600
    assert _JOB_DEFAULTS["coalesce"] is True
    assert _JOB_DEFAULTS["max_instances"] == 1
    now = ist_now()
    assert now.utcoffset().total_seconds() == 5.5 * 3600


def test_save_json_atomic_roundtrip(tmp_path):
    from src.stock_common import save_json
    p = tmp_path / "x.json"
    save_json(p, {"a": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
    leftovers = [q for q in tmp_path.iterdir() if q.name != "x.json"]
    assert not leftovers, "temp files must not survive a successful write"
