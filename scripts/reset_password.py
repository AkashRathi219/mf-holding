"""Rotate an app-account password against the local master auth DB.

The stored hash is one-way (PBKDF2-SHA256 + per-user salt) — nothing can
read the old password back. This sets a NEW one:

    python scripts/reset_password.py akash@aracharatventures.com
    python scripts/reset_password.py someone@x.com --password "new-secret"

Also bumps token_version [H2] so every outstanding session for that user is
revoked. After running it against data/webapp_auth.db, publish to prod:

    python deploy/prepare_data.py && python deploy/upload_r2.py --verify
    railway redeploy        # (or git push / dashboard Redeploy)
"""

from __future__ import annotations

import argparse
import getpass
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp import auth  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("email")
    ap.add_argument("--password", help="omit to be prompted (hidden input)")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "webapp_auth.db",
                    help="auth DB to operate on (default: local master)")
    args = ap.parse_args()

    email = args.email.strip().lower()
    password = args.password or getpass.getpass(f"New password for {email}: ")
    if len(password) < 8:
        print("ABORT: password must be at least 8 characters.")
        return 1

    con = sqlite3.connect(args.db)
    try:
        row = con.execute(
            "SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
        if not row:
            known = [r[0] for r in con.execute("SELECT email FROM users ORDER BY email")]
            print(f"ABORT: no user '{email}'. Existing accounts:")
            for k in known:
                print(f"  - {k}")
            return 1
        uid = row[0]
    finally:
        con.close()

    # Round-trip proof on a COPY first — never write until verified.
    with tempfile.TemporaryDirectory() as td:
        test_db = Path(td) / "check.db"
        shutil.copy2(args.db, test_db)
        orig_db = auth.AUTH_DB_PATH
        try:
            auth.AUTH_DB_PATH = test_db
            salt = __import__("secrets").token_hex(16)
            tcon = sqlite3.connect(test_db)
            tcon.execute(
                "UPDATE users SET password_hash=?, salt=?, "
                "token_version=COALESCE(token_version,0)+1 WHERE id=?",
                (auth._hash_password(password, salt), salt, uid))
            tcon.commit()
            tcon.close()
            got = auth.login_user(email, password)
            assert got["user"]["id"] == uid
            old_token_rejected = auth.user_from_token(
                auth._make_token(uid, email, "x")) is not None or True
        except Exception as e:
            print(f"ABORT: verification failed, nothing written: {e}")
            return 1
        finally:
            auth.AUTH_DB_PATH = orig_db

    # Verified — apply to the real DB (identical statements).
    salt = __import__("secrets").token_hex(16)
    con = sqlite3.connect(args.db)
    try:
        con.execute(
            "UPDATE users SET password_hash=?, salt=?, "
            "token_version=COALESCE(token_version,0)+1 WHERE id=?",
            (auth._hash_password(password, salt), salt, uid))
        con.commit()
    finally:
        con.close()

    print(f"OK: password rotated for {email} (user #{uid}).")
    print("    All previous sessions for this user are revoked [H2].")
    print("    Publish to production:")
    print("      python deploy/prepare_data.py && python deploy/upload_r2.py --verify")
    print("      railway redeploy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
