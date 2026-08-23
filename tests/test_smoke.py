"""Smoke suite: app boots, every GET route stays non-5xx, key endpoints sane."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

SUPERADMIN = {"name": "Test Super", "email": "super@test.local",
              "org": "t", "password": "password123"}


@pytest.fixture(scope="module")
def client():
    from webapp.main import app
    with TestClient(app) as c:  # startup hook runs; scheduler disabled in tests
        yield c


@pytest.fixture(scope="module")
def admin_headers(client):
    from conftest import ensure_token
    return ensure_token(client, SUPERADMIN)


# ---- targeted endpoint checks ----------------------------------------------

def test_version_endpoint(client):
    r = client.get("/api/version")  # [S1] was a deterministic NameError -> 500
    assert r.status_code == 200, r.text
    body = r.json()
    assert "server_time_ist" in body and "T" in body["server_time_ist"]
    assert "scheduler_enabled" in body


def test_health_deep_probe(client):
    r = client.get("/api/health")  # [S3] no longer a static literal
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"]["ok"] is True
    assert body["checks"]["db"]["schemes"] >= 1


def test_health_503_when_db_dead_in_prod_intent(client, monkeypatch):
    from webapp import main

    def broken(force: bool = False):
        raise sqlite_err()

    def sqlite_err():
        import sqlite3
        return sqlite3.OperationalError("no such db")

    monkeypatch.setenv("MF_READONLY_DB", "1")  # prod-intent declared [S3]
    monkeypatch.setattr(main.db, "get_db", broken)
    monkeypatch.setattr(main, "_db", None)
    monkeypatch.setattr(main, "_health_cache", None)
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["checks"]["db"]["ok"] is False


def test_health_degraded_ok_without_prod_intent(client, monkeypatch):
    """Dev checkout with no data and no R2 must NOT crash-loop: 200 + flags."""
    from webapp import main

    def empty(force: bool = False):
        class _Empty:
            con = type("C", (), {"execute": staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(sqlite_err()))})()

        def sqlite_err():
            import sqlite3
            return sqlite3.OperationalError("no such table")

        raise sqlite_err()

    monkeypatch.delenv("MF_READONLY_DB", raising=False)
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(main.db, "get_db", empty)
    monkeypatch.setattr(main, "_db", None)
    monkeypatch.setattr(main, "_health_cache", None)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["db"]["ok"] is False


def test_scope_stats_public(client):
    r = client.get("/api/scope-stats")
    assert r.status_code == 200
    assert r.json()["schemes"] >= 1


def test_request_id_header(client):  # [F8]
    r = client.get("/api/version")
    assert r.headers.get("X-Request-ID")


def test_static_assets(client):
    assert client.get("/").status_code == 200
    assert client.get("/app").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


# ---- full GET sweep ---------------------------------------------------------
# Excluded (need live data files beyond the sandbox; covered manually /
# in later phases): /api/admin/data-health*, /api/admin/reliance,
# /api/stocks/status.

_PATH_PARAMS: dict[str, str] = {
    "scheme_id": "1", "isin": "INE002A01018", "page_name": "",
}


def _concrete_path(path: str) -> str | None:
    for token in path.split("/"):
        if token.startswith("{") and token.endswith("}"):
            val = _PATH_PARAMS.get(token[1:-1])
            if val is None:
                return None
            path = path.replace(token, val)
    return path


def test_every_get_route_non_5xx_authed(client, admin_headers):
    failures: list[str] = []
    seen = 0
    for route in client.app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or "GET" not in methods or not path:
            continue
        if any(x in path for x in ("data-health", "reliance", "stocks/status")):
            continue
        url = _concrete_path(path)
        if url is None:
            continue
        seen += 1
        r = client.get(url, headers=admin_headers)
        if r.status_code >= 500:
            failures.append(f"{url} -> {r.status_code}: {r.text[:160]}")
    assert seen > 15, f"sweep suspiciously small: {seen}"
    assert not failures, "GET routes returned 5xx:\n" + "\n".join(failures)


def test_every_get_route_non_5xx_anonymous(client):
    failures: list[str] = []
    for route in client.app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or "GET" not in methods or not path:
            continue
        if any(x in path for x in ("data-health", "reliance", "stocks/status")):
            continue
        url = _concrete_path(path)
        if url is None:
            continue
        r = client.get(url)
        if r.status_code >= 500:
            failures.append(f"{url} -> {r.status_code}")
    assert not failures, "anonymous GET routes returned 5xx:\n" + "\n".join(failures)
