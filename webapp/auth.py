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
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DB_PATH = BASE_DIR / "data" / "webapp_auth.db"
SECRET_PATH = Path(__file__).resolve().parent / ".secret_key"

_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
_PBKDF2_ITERATIONS = 200_000

_EMAIL_RE = None  # simple validation below; keep it dependency-free


def _get_secret() -> bytes:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"].encode()
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    secret = secrets.token_bytes(48)
    SECRET_PATH.write_bytes(secret)
    return secret


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
            created_at REAL
        )"""
    )
    con.commit()
    return con


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), _PBKDF2_ITERATIONS
    ).hex()


def _make_token(user_id: int, email: str, name: str) -> str:
    secret = _get_secret()
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": user_id, "email": email, "name": name,
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
    return {"token": token, "user": {"id": uid, "email": email, "name": name, "org": org}}


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
    return {"token": token, "user": {"id": uid, "email": db_email, "name": name, "org": org}}


def user_from_token(token: str) -> dict | None:
    payload = verify_token(token)
    if not payload:
        return None
    con = _conn()
    try:
        row = con.execute(
            "SELECT id, email, name, org FROM users WHERE id=?",
            (payload.get("uid"),)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    uid, email, name, org = row
    return {"id": uid, "email": email, "name": name, "org": org}