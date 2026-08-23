"""WEB2: /api/try/waitlist — public signup, rate limited, R2-restorable."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def wl_path(tmp_path, monkeypatch):
    from webapp import main
    p = tmp_path / "waitlist.json"
    monkeypatch.setattr(main, "WAITLIST_PATH", p)
    monkeypatch.setattr(main, "_health_cache", None)
    main.WAITLIST_LIMITER.reset()
    return p


@pytest.fixture(scope="module")
def client():
    from webapp.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def users(client):
    from conftest import ensure_token
    sa = ensure_token(client, {"name": "SA", "email": "super@test.local",
                               "org": "", "password": "password123"})
    plain = ensure_token(client, {"name": "Plain", "email": "plain@test.local",
                                  "org": "", "password": "password123"})
    return {"sa": sa, "plain": plain}


def _post(client, email="a@example.com", **extra):
    return client.post("/api/try/waitlist", json={"email": email, **extra})


def test_public_no_auth_required(client, wl_path):
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


def test_email_validation(client, wl_path):
    # exactly 5 probes: the shared limiter allows 5/hour per IP
    assert _post(client, "").status_code == 400
    assert _post(client, "not-an-email").status_code == 400
    assert _post(client, "two@@example.com").status_code == 400
    assert _post(client, "@example.com").status_code == 400
    assert _post(client, "user@").status_code == 400
    assert not wl_path.exists()  # nothing persisted for invalid input


def test_roundtrip_atomic_and_idempotent(client, wl_path):
    assert _post(client, "first@example.com", source="test").status_code == 200
    assert not wl_path.with_suffix(".json.tmp").exists()  # atomic replace
    doc = json.loads(wl_path.read_text(encoding="utf-8"))
    assert doc[0]["email"] == "first@example.com"
    assert doc[0]["source"] == "test"
    # repeat signup: ok, no duplicate row
    again = _post(client, "FIRST@example.com")
    assert again.status_code == 200 and again.json()["total"] >= 1
    emails = [r["email"] for r in json.loads(
        wl_path.read_text(encoding="utf-8"))]
    assert emails.count("first@example.com") == 1


def test_admin_reader_scoped(client, users, wl_path):
    _post(client, "r-one@example.com")
    ok = client.get("/api/admin/waitlist", headers=users["sa"])
    assert ok.status_code == 200 and ok.json()["total"] >= 1
    denied = client.get("/api/admin/waitlist", headers=users["plain"])
    assert denied.status_code == 403


def test_rate_limited_per_ip(client, wl_path, monkeypatch):
    from webapp import main
    main.WAITLIST_LIMITER.reset()
    for i in range(5):  # max_events per hour
        assert _post(client, f"u{i}@example.com").status_code == 200
    assert _post(client, "over@example.com").status_code == 429
    main.WAITLIST_LIMITER.reset()


def test_reader_restores_from_r2_copy(client, users, wl_path, monkeypatch):
    """Cold container: local file absent -> reader pulls the R2 copy."""
    from webapp import main
    wl_path.unlink(missing_ok=True)

    def fake_ensure(key, dest=None):
        dest.write_text(json.dumps([{"at": "t", "email": "restored@x.com",
                                     "source": "r2"}]), encoding="utf-8")
        return dest

    monkeypatch.setattr(main, "remote_ensure", fake_ensure)
    got = client.get("/api/admin/waitlist", headers=users["sa"])
    assert got.status_code == 200
    assert any(i["email"] == "restored@x.com" for i in got.json()["items"])
