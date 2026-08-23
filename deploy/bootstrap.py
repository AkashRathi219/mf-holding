"""Railway boot bootstrap: pull the runtime data from R2 before uvicorn starts.

Reads the uploaded ``db/manifest.json`` and downloads ONLY the boot-critical
files (webapp.db, bonds catalog, CAS sample) plus everything the spot-check
fails for. Everything else is lazy-fetched per-request by ``webapp.remote_store``.

Safety: MF_READONLY_DB=1 and R2 vars needed. Without them, boot is a no-op
(behaves like the local dev app). Can be re-run any time.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.remote_store import download_to, ensure_prefix  # noqa: E402

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
    r2_vars = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
               "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    have_r2 = all(os.environ.get(k) for k in r2_vars)

    if os.environ.get("MF_READONLY_DB") != "1":
        print("MF_READONLY_DB != 1, skipping bootstrap")
        return 0

    if not have_r2:
        # [FIX] readonly mode WITHOUT R2 means webapp.db can never appear ->
        # uvicorn would start into a guaranteed-failing healthcheck. Fail the
        # deploy here where the cause is obvious.
        print("=" * 64)
        print("BOOTSTRAP FAILED — MF_READONLY_DB=1 requires the R2 variables:")
        missing = [k for k in r2_vars if not os.environ.get(k)]
        for k in missing:
            print(f"  MISSING: {k}")
        print("Set them in the Railway service Variables tab.")
        print("=" * 64)
        return 1

    ok = fail = 0
    failed_keys: list[str] = []
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
                failed_keys.append(key)

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
        # [FIX] A failed boot-critical fetch used to be a warning and uvicorn
        # started into an empty database — which the /api/health deep probe
        # then rejected forever (Railway crash-loop with an opaque
        # "health check failed"). Fail the deploy HERE where the reason is
        # visible: wrong/missing R2 credentials are the usual cause.
        print("=" * 64)
        print("BOOTSTRAP FAILED — deploy cannot serve traffic.")
        print("Failed boot-critical files:")
        for k in failed_keys:
            print(f"  - {k}")
        print("Check the Railway variables:")
        print("  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,")
        print("  R2_BUCKET, MF_READONLY_DB=1")
        print("=" * 64)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
