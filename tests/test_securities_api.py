"""Securities API surface: /api/securities/{isin}/financials contract.

Regression guard for the Statements-tab P0 (the UI fetched
/api/securities/{isin}/statements — a route that never existed, so the tab
always 404'd): the backend segment is 'financials', and app.js must map the
'statements' tab id onto it while keeping the UI label/cache key unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
ISIN = "INE002A01018"  # seeded by conftest.py


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


@pytest.fixture(scope="module")
def client():
    from webapp.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    from conftest import ensure_token

    return ensure_token(client, {"name": "Sec", "email": "sec@test.local",
                                 "org": "", "password": "password123"})


@pytest.fixture()
def statements_doc(tmp_path, monkeypatch):
    from webapp import db as wdb

    d = tmp_path / "stock_financials"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "STOCK_FINANCIALS_DIR", d)
    doc = {
        "isin": ISIN,
        "symbol": "TESTIND",
        "name": "Test Industries Ltd",
        "consolidated": {
            "currency": "INR",
            "quarters": [
                {"period_end": "2026-06-30", "revenue": 100.0,
                 "net_profit": 10.0, "eps": 2.0},
            ],
        },
    }
    (d / f"{ISIN}.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_financials_requires_auth(client):
    assert client.get(f"/api/securities/{ISIN}/financials").status_code == 401


def test_financials_unknown_isin_404(client, auth):
    r = client.get("/api/securities/INE999999999/financials",
                   headers=auth)
    assert r.status_code == 404


def test_financials_returns_normalised_doc(client, auth, statements_doc):
    r = client.get(f"/api/securities/{ISIN}/financials", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["isin"] == ISIN
    assert body["consolidated"]["quarters"][0]["period_end"] == "2026-06-30"


def test_financials_without_file_is_available_false(client, auth,
                                                    tmp_path, monkeypatch):
    from webapp import db as wdb

    monkeypatch.setattr(wdb, "STOCK_FINANCIALS_DIR", tmp_path / "none")
    r = client.get(f"/api/securities/{ISIN}/financials", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body.get("note")


def test_app_js_maps_statements_tab_to_financials_route():
    src = (ROOT / "webapp" / "static" / "js" / "app.js").read_text(
        encoding="utf-8")
    assert 'tab === "statements" ? "financials" : tab' in src
    # the old broken fetch shape must be gone for good
    assert "/${tab}`" not in src.split("function switchStockTab")[1]
    # the UI keeps its own tab id / cache key: only the URL segment changes
    assert '["statements", "Statements"]' in src


def test_technical_requires_auth(client):
    assert client.get(f"/api/securities/{ISIN}/technical").status_code == 401


def test_technical_unknown_isin_404(client, auth):
    r = client.get("/api/securities/INE999999999/technical", headers=auth)
    assert r.status_code == 404


def test_security_analytics_page_contract():
    """Full-page analytics view (#security/<ISIN>/analytics): hash route,
    screen section, lazy tab loader and the entry-point links from the
    directory row + the security-detail card must all stay wired."""
    js = (ROOT / "webapp" / "static" / "js" / "app.js").read_text(
        encoding="utf-8")
    html = (ROOT / "webapp" / "static" / "app.html").read_text(
        encoding="utf-8")
    assert "/^security\\/([A-Z0-9]+)\\/analytics$/i" in js
    assert 'id="screen-secanalytics"' in html
    assert 'id="secAnalyticsBody"' in html
    assert "renderSecurityAnalytics(" in js
    assert "switchAnalyticsTab(" in js
    assert "renderAnalyticsTab(" in js
    # entry points: detail-page card button + per-row shortcut
    assert "/analytics\"" in js or "/analytics`" in js
    assert "Open full analytics" in js
