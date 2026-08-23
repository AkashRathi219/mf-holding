"""Phase 2 security hardening: rate limiting [H1], token revocation [H2],
CORS allowlist [H3], secret hygiene [H4]."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from webapp.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_limiters():
    from webapp import ratelimit as rl
    rl.AUTH_LOGIN_LIMITER.reset()
    rl.AUTH_REGISTER_LIMITER.reset()
    yield
    rl.AUTH_LOGIN_LIMITER.reset()
    rl.AUTH_REGISTER_LIMITER.reset()


def _register(client, email):
    return client.post("/api/auth/register", json={
        "name": "RL", "email": email, "org": "", "password": "password123"})


# ---- H1: rate limiting ------------------------------------------------------

def test_login_rate_limited(client):
    for i in range(10):  # limit: 10 per 5 min per IP
        r = client.post("/api/auth/login", json={
            "email": f"ghost{i}@noone.test", "password": "whatever123"})
        assert r.status_code == 401, i
    r = client.post("/api/auth/login", json={
        "email": "ghost11@noone.test", "password": "whatever123"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_register_rate_limited(client):
    for i in range(5):  # limit: 5 per hour per IP
        r = _register(client, f"regburst{i}@test.local")
        assert r.status_code == 200, (i, r.text)
    r = _register(client, "regburst5@test.local")
    assert r.status_code == 429


def test_rate_limit_is_per_ip(client):
    from webapp import ratelimit as rl
    for i in range(10):
        rl.AUTH_LOGIN_LIMITER.check("1.2.3.4")
    rl.AUTH_LOGIN_LIMITER.check("5.6.7.8")  # different key -> unaffected


def test_forwarded_for_honoured():
    from webapp.ratelimit import client_ip

    class Req:  # minimal stand-in
        headers = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
        client = None

    assert client_ip(Req()) == "203.0.113.7"


# ---- H2: token revocation ---------------------------------------------------

def test_logout_all_revokes_existing_tokens(client):
    tok = _register(client, "revme@test.local").json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/auth/me", headers=h).status_code == 200

    r = client.post("/api/auth/logout-all", headers=h)
    assert r.status_code == 200 and r.json()["revoked"] is True

    # signature still verifies, but the version check kills the session [H2]
    from webapp import auth
    assert auth.verify_token(tok) is not None
    assert client.get("/api/auth/me", headers=h).status_code == 401

    # fresh login mints a token at the NEW version -> works again
    tok2 = client.post("/api/auth/login", json={
        "email": "revme@test.local", "password": "password123"}).json()["token"]
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {tok2}"}).status_code == 200


def test_pre_revocation_token_fails_after_passwordless_bump(client):
    """Direct DB bump (e.g. after a compromise report) invalidates old tokens."""
    from webapp import auth
    tok = _register(client, "bumpme@test.local").json()["token"]
    uid = auth._conn().execute(
        "SELECT id FROM users WHERE email='bumpme@test.local'").fetchone()[0]
    auth.revoke_user_tokens(uid)
    assert auth.user_from_token(tok) is None


# ---- H3: CORS ----------------------------------------------------------------

def _mini_app(origins: str) -> TestClient:
    from webapp.main import _add_cors
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    _add_cors(app, origins=origins)
    return TestClient(app)


def test_cors_allowlisted_origin_gets_headers():
    c = _mini_app("https://aracharatventures.com, https://www.aracharatventures.com")
    pre = c.options("/ping", headers={
        "Origin": "https://aracharatventures.com",
        "Access-Control-Request-Method": "GET"})
    assert pre.status_code in (200, 204)
    assert pre.headers["access-control-allow-origin"] == "https://aracharatventures.com"
    got = c.get("/ping", headers={"Origin": "https://www.aracharatventures.com"})
    assert got.headers["access-control-allow-origin"] == "https://www.aracharatventures.com"


def test_cors_unknown_origin_gets_nothing():
    c = _mini_app("https://aracharatventures.com")
    got = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in got.headers


def test_cors_disabled_by_default():
    c = _mini_app("")
    got = c.get("/ping", headers={"Origin": "https://anything.example"})
    assert "access-control-allow-origin" not in got.headers


# ---- H4: secret hygiene ------------------------------------------------------

@pytest.fixture()
def fresh_secret_env(monkeypatch, tmp_path):
    """Isolated secret resolution: no env key, empty tmp dir, cold cache."""
    from webapp import auth
    orig_path = auth.SECRET_PATH
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("REQUIRE_SECRET_KEY", raising=False)
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / ".secret_key")
    auth.reset_secret_cache()
    yield auth
    auth.reset_secret_cache()  # never leak state into other tests
    monkeypatch.setattr(auth, "SECRET_PATH", orig_path)


def test_secret_cached_per_process(fresh_secret_env, monkeypatch):
    auth = fresh_secret_env
    monkeypatch.setenv("SECRET_KEY", "secret-one")
    assert auth._get_secret() == b"secret-one"
    monkeypatch.setenv("SECRET_KEY", "secret-two")
    assert auth._get_secret() == b"secret-one", "cache must serve first value"
    auth.reset_secret_cache()
    assert auth._get_secret() == b"secret-two"


def test_secret_missing_in_prod_fails_loudly(fresh_secret_env, monkeypatch):
    auth = fresh_secret_env
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        auth._get_secret()


def test_secret_dev_fallback_generates(fresh_secret_env):
    auth = fresh_secret_env
    secret = auth._get_secret()
    assert isinstance(secret, bytes) and len(secret) == 48
