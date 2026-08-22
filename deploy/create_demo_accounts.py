"""Create demo accounts + seed sample data into the local auth/userdata DBs.

Run from repo root:  python deploy/create_demo_accounts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import auth, userdata
from webapp.seed_samples import seed_for_user

ACCOUNTS = [
    {"email": "demo@factsheet.ai", "password": "Demo#2026",
     "name": "Demo Advisor", "org": "Factsheet Demo"},
    {"email": "advisor@factsheet.ai", "password": "Advisor#2026",
     "name": "Senior Advisor", "org": "Factsheet Demo"},
    {"email": "viewer@factsheet.ai", "password": "Viewer#2026",
     "name": "Read-only Viewer", "org": "Factsheet Demo"},
]


def main() -> int:
    for acc in ACCOUNTS:
        try:
            reg = auth.register_user(acc["email"], acc["name"], acc["org"], acc["password"])
            uid = int(reg["user"]["id"])
            print(f"created {acc['email']} (uid {uid})")
        except Exception as e:  # already registered -> log in instead
            try:
                login = auth.login_user(acc["email"], acc["password"])
                uid = int(login["user"]["id"])
                print(f"exists   {acc['email']} (uid {uid})")
            except Exception as e2:
                print(f"FAILED   {acc['email']}: {e} / {e2}")
                continue
        seeded = seed_for_user(uid, reset=True)
        print("   seeded:", {k: len(v) for k, v in seeded.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
