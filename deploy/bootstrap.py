"""Railway boot bootstrap: pull the runtime data from R2 before uvicorn starts.

Reads the uploaded ``db/manifest.json`` and downloads ONLY the boot-critical
files (webapp.db, bonds catalog, CAS sample) plus everything the spot-check
fails for. Everything else is lazy-fetched per-request by ``webapp.remote_store``.

Safety: MF_READONLY_DB=1 and R2 vars needed. Without them, boot is a no-op
(behaves like the local dev app). Can be re-run any time.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MANIFEST_KEY = "db/manifest.json"


def load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v and not os.environ.get(k):
            os.environ[k] = v

# Files we must have BEFORE the server accepts traffic.
BOOT_CRITICAL = [
    "webapp.db",
    "reference/bonds_catalog.json",
    "CAS_sample_portfolio_holdings.json",
    "stocks/identity.json",
    "userdata.db",       # strategies / models / clients / portfolios
    "webapp_auth.db",    # registered accounts (demo + real)
]


def main() -> int:
    t0 = time.time()
    load_env()
    if os.environ.get("MF_READONLY_DB") != "1":
        print("MF_READONLY_DB != 1, skipping bootstrap")
        return 0

    if not all(os.environ.get(k) for k in (
            "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")):
        print("R2 env vars missing, skipping bootstrap")
        return 0

    from webapp.remote_store import download_to, ensure_prefix

    ok = fail = 0
    for key in BOOT_CRITICAL:
        dest = DATA / key
        got = download_to(key, dest)
        if got:
            ok += 1
            print(f"  {key:48s} {dest.stat().st_size / 1e6:7.1f} MB")
        else:
            if dest.exists():
                print(f"  {key:48s} (present locally)")
                ok += 1
            else:
                print(f"  {key:48s} FAILED")
                fail += 1

    # Small runtime dirs the daily jobs read wholesale (universe CSV/navall).
    try:
        pulled = ensure_prefix("universe")
        if pulled:
            print(f"  universe/                                          +{pulled} files")
            ok += 1
    except Exception as e:  # noqa: BLE001
        print(f"  universe pull failed: {e}")

    if ok:
        print(f"bootstrap ok={ok} fail={fail} in {time.time() - t0:.1f}s")
    if fail:
        print("WARNING: some boot-critical files failed to fetch; "
              "starting uvicorn anyway (app may have degraded features)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
