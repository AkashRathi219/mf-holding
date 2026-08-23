"""S4: feedback durability — atomic, lock-protected, R2-restorable + reader."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def fb_path(tmp_path, monkeypatch):
    from webapp import main
    p = tmp_path / "feedback.json"
    monkeypatch.setattr(main, "FEEDBACK_PATH", p)
    monkeypatch.setattr(main, "_health_cache", None)
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


def _post(client, headers, msg="hello", ctx="test"):
    return client.post("/api/feedback", headers=headers,
                       json={"message": msg, "context": ctx})


def test_feedback_requires_auth(client):
    assert client.post("/api/feedback", json={"message": "x"}).status_code == 401
    assert client.post("/api/feedback", json={"message": ""}).status_code == 401


def test_feedback_roundtrip_and_atomicity(client, users, fb_path):
    r1 = _post(client, users["sa"], "first")
    assert r1.status_code == 200, r1.text
    assert r1.json()["total"] == 1
    tmp = fb_path.with_suffix(".json.tmp")
    assert not tmp.exists()          # atomic replace leaves no temp behind
    doc = json.loads(fb_path.read_text(encoding="utf-8"))
    assert doc[0]["message"] == "first"

    r2 = _post(client, users["sa"], "second")
    assert r2.json()["total"] == 2   # appends, never overwrites [S4]
    assert len(json.loads(fb_path.read_text(encoding="utf-8"))) == 2


def test_admin_reader_scoped(client, users, fb_path):
    _post(client, users["sa"], "r-one")
    _post(client, users["sa"], "r-two")
    ok = client.get("/api/admin/feedback",
                    headers=users["sa"])
    assert ok.status_code == 200 and ok.json()["total"] >= 2
    denied = client.get("/api/admin/feedback",
                        headers=users["plain"])
    assert denied.status_code == 403


def test_reader_restores_from_r2_copy(client, users, fb_path, monkeypatch):
    """Cold container: local file absent -> reader pulls the R2 copy."""
    from webapp import main
    fb_path.unlink(missing_ok=True)

    def fake_ensure(key, dest=None):
        dest.write_text(json.dumps([{"at": "t", "user": "r2", "message": "restored",
                                     "context": "c"}]), encoding="utf-8")
        return dest

    monkeypatch.setattr(main, "remote_ensure", fake_ensure)
    got = client.get("/api/admin/feedback", headers=users["sa"])
    assert got.status_code == 200
    assert any(i["message"] == "restored" for i in got.json()["items"])
