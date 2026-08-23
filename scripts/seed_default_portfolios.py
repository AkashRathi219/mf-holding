"""Seed the default CAS dataset into ALL local accounts.

Runs ``seed_for_user`` (non-destructive: existing rows are kept, missing rows
are created) for the superadmin and every demo/advisor account found in
data/webapp_auth.db. Use after changing seed_samples.py to backfill accounts
that already exist, then publish:

    python deploy/prepare_data.py && python deploy/upload_r2.py --verify
    railway redeploy        # (or git push)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp import auth  # noqa: E402
from webapp.seed_samples import CLIENT1_NAME, DEFAULT_PORTFOLIO_NAME, seed_for_user  # noqa: E402

SUPERADMIN_EMAIL = "akash@aracharatventures.com"
DEMO_EMAILS = ["demo@factsheet.ai", "advisor@factsheet.ai", "viewer@factsheet.ai"]


def main() -> int:
    con = auth._conn()
    try:
        rows = con.execute("SELECT id, email FROM users ORDER BY id").fetchall()
    finally:
        con.close()
    targets = {e.lower(): i for i, e in rows}
    wanted = [SUPERADMIN_EMAIL] + DEMO_EMAILS

    rc = 0
    for email in wanted:
        uid = targets.get(email)
        if not uid:
            print(f"SKIP {email}: no such account")
            rc = 1
            continue
        created = seed_for_user(uid)
        ok = (CLIENT1_NAME in created["clients"]
              and DEFAULT_PORTFOLIO_NAME in created["portfolios"])
        print(f"uid {uid:<3} {email:<32} created={created}"
              if ok else
              f"uid {uid:<3} {email:<32} already present / partial: {created}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
