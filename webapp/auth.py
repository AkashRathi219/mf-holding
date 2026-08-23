"""Authentication for the Factsheet Engine AI webapp.

Stateless, dependency-free auth:

- Users stored in a small SQLite DB (``data/webapp_auth.db``).
- Passwords hashed with PBKDF2-HMAC-SHA256 + per-user random salt (stdlib only).
- Sessions are HMAC-signed tokens (``header.payload.signature``) using a secret
  key generated once and persisted to ``webapp/.secret_key``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DB_PATH = BASE_DIR / "data" / "webapp_auth.db"
SECRET_PATH = Path(__file__).resolve().parent / ".secret_key"

_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
_PBKDF2_ITERATIONS = 200_000

_EMAIL_RE = None  # simple validation below; keep it dependency-free

_secret_cache: bytes | None = None
_secret_lock = threading.Lock()


def _get_secret() -> bytes:
    """Token-signing secret, stable across redeploys.

    Order: SECRET_KEY env var -> local .secret_key file -> R2 copy (pulled via
    remote_store so Railway containers reuse the same key) -> generate + save.

    [H4] The resolved secret is cached for the process lifetime (was: one disk
    read per authenticated request). In a production environment a missing
    secret FAILS LOUDLY instead of silently auto-generating — an auto-generated
    secret invalidates every existing session on the next process restart.
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    with _secret_lock:
        if _secret_cache is not None:
            return _secret_cache
        secret = _resolve_secret()
        _secret_cache = secret
        return secret


def _resolve_secret() -> bytes:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"].encode()
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    try:
        from .remote_store import ensure
        got = ensure("webapp/.secret_key", dest=SECRET_PATH)
        if got and SECRET_PATH.exists():
            return SECRET_PATH.read_bytes()
    except Exception:
        pass  # fall through
    if _is_prod():
        raise RuntimeError(
            "SECRET_KEY is not configured but the environment is production. "
            "Set the SECRET_KEY env var (or provide webapp/.secret_key via R2) "
            "— refusing to auto-generate, which would invalidate all sessions.")
    secret = secrets.token_bytes(48)
    try:
        SECRET_PATH.write_bytes(secret)
    except OSError:
        pass
    return secret


def _is_prod() -> bool:
    """Railway sets RAILWAY_ENVIRONMENT_NAME=production on prod services."""
    env = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").lower()
    return env == "production" or os.environ.get("REQUIRE_SECRET_KEY") == "1"


def reset_secret_cache() -> None:
    """Test hook: forget the cached secret so env changes take effect."""
    global _secret_cache
    with _secret_lock:
        _secret_cache = None


def _conn() -> sqlite3.Connection:
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(AUTH_DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            org TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at REAL,
            token_version INTEGER DEFAULT 0
        )"""
    )
    # [H2] migrate pre-existing DBs that lack the revocation column.
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
    if "token_version" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0")
    con.commit()
    return con


def _token_version(user_id: int) -> int:
    con = _conn()
    try:
        row = con.execute(
            "SELECT COALESCE(token_version, 0) FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else -1
    finally:
        con.close()


def revoke_user_tokens(user_id: int) -> None:
    """Kill switch [H2]: bump the user's token_version so every outstanding
    token signed before now fails verification."""
    con = _conn()
    try:
        con.execute(
            "UPDATE users SET token_version=COALESCE(token_version,0)+1 WHERE id=?",
            (user_id,))
        con.commit()
    finally:
        con.close()


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), _PBKDF2_ITERATIONS
    ).hex()


def _make_token(user_id: int, email: str, name: str) -> str:
    secret = _get_secret()
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": user_id, "email": email, "name": name,
                    "tv": _token_version(user_id),
                    "exp": int(time.time()) + _TOKEN_TTL_SECONDS}).encode()
    ).rstrip(b"=")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return f"{payload.decode()}.{sig_b64.decode()}"


def verify_token(token: str) -> dict | None:
    try:
        payload_b64, sig_b64 = token.rsplit(".", 1)
        secret = _get_secret()
        expected = base64.urlsafe_b64encode(
            hmac.new(secret, payload_b64.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, sig_b64):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


class AuthError(Exception):
    pass


def register_user(email: str, name: str, org: str, password: str) -> dict:
    email = (email or "").strip().lower()
    name = (name or "").strip()
    org = (org or "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("Please enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if not name:
        raise AuthError("Please enter your name.")
    salt = secrets.token_hex(16)
    con = _conn()
    try:
        cur = con.execute("SELECT id FROM users WHERE email=?", (email,))
        if cur.fetchone():
            raise AuthError("An account with this email already exists.")
        con.execute(
            "INSERT INTO users (email, name, org, password_hash, salt, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (email, name, org, _hash_password(password, salt), salt, time.time()))
        con.commit()
        uid = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    finally:
        con.close()
    token = _make_token(uid, email, name)
    return {"token": token,
            "user": {"id": uid, "email": email, "name": name, "org": org},
            "superadmin": email in _superadmin_emails()}


def _superadmin_emails() -> set[str]:
    import os
    return {e.strip().lower() for e in os.environ.get(
        "SUPERADMIN_EMAILS", "akash@aracharatventures.com").split(",") if e.strip()}


def login_user(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    if not email or not password:
        raise AuthError("Email and password are required.")
    con = _conn()
    try:
        row = con.execute(
            "SELECT id, email, name, org, password_hash, salt FROM users WHERE email=?",
            (email,)).fetchone()
    finally:
        con.close()
    if not row:
        raise AuthError("Invalid email or password.")
    uid, db_email, name, org, pw_hash, salt = row
    if not hmac.compare_digest(_hash_password(password, salt), pw_hash):
        raise AuthError("Invalid email or password.")
    token = _make_token(uid, db_email, name)
    return {"token": token,
            "user": {"id": uid, "email": db_email, "name": name, "org": org},
            "superadmin": db_email.lower() in _superadmin_emails()}


def user_from_token(token: str) -> dict | None:
    payload = verify_token(token)
    if not payload:
        return None
    con = _conn()
    try:
        row = con.execute(
            "SELECT id, email, name, org, COALESCE(token_version,0) FROM users WHERE id=?",
            (payload.get("uid"),)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    uid, email, name, org, tv = row
    # [H2] revocation: tokens signed before a token_version bump are dead.
    if int(payload.get("tv", 0)) != int(tv):
        return None
    return {"id": uid, "email": email, "name": name, "org": org}