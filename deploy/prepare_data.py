"""Stage the runtime data + a freshly built webapp.db for the R2 upload (step 1).

Outputs ``deploy/data/`` -- a self-contained snapshot of only the files the
Railway webapp needs at runtime -- plus ``deploy/manifest.json`` listing every
file (sha256, size, s3 key) for the bootstrap loader.

Usage (from repo root)::

    python deploy/prepare_data.py

Safe to re-run: it rebuilds ``deploy/data`` from scratch each invocation. It
never writes to ``data/`` (the live local DB is untouched).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "deploy" / "data"
MANIFEST = ROOT / "deploy" / "manifest.json"

# Directories under data/ the running webapp reads at request time.
RUNTIME_DIRS = [
    "nav_history",       # funds  : per-scheme AMFI NAV history (charts)
    "stock_history",     # stocks : daily closes per ISIN
    "stock_actions",     # stocks : dividends / splits per ISIN
    "stock_reports",     # stocks : financial-report announcements per ISIN
    "stocks",            # stocks : identity.json (ISIN -> NSE symbol)
    "reference",         # bonds/bonds_catalog.json + isin_latest_nav.json + misc
    "nifty",             # nifty/weights.json (index fund weights)
    "universe",          # navall.txt + Combined NAV CSV (DB seed inputs)
    "parsed",            # amfi/amc_websites/advisorkhoj holdings JSONs
]

# Files under data/ that are needed but live outside the dirs above.
RUNTIME_FILES = [
    "data/feedback.json",
]

# Extra root-level files (repo root, not under data/).
ROOT_FILES = [
    "CAS_sample_portfolio_holdings.json",
]

# data/ content known to be pipeline-only clutter -- never staged.
SKIP_DIRS = {"raw", "stock_bhavcopy", "pdfs", "logs", "downloads", ".staging"}

DBS = ["data/webapp.db", "data/userdata.db", "data/webapp_auth.db"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: Path, dst: Path, skip: set[str]) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in skip:
            continue
        if child.is_dir():
            copy_tree(child, dst / child.name, skip)
        elif child.is_file():
            shutil.copy2(child, dst / child.name)


def main() -> None:
    t0 = time.time()
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    for d in RUNTIME_DIRS:
        copy_tree(ROOT / "data" / d, STAGE / d, SKIP_DIRS)
    for f in RUNTIME_FILES:
        p = ROOT / f
        if p.exists():
            dst = STAGE / p.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    for f in ROOT_FILES:
        p = ROOT / f
        if p.exists():
            shutil.copy2(p, STAGE / p.name)

    db = ROOT / "data" / "webapp.db"
    if not db.exists():
        print(f"  !! {db} missing -- build it first, then re-run")
        sys.exit(1)
    # All three databases ship: holdings cache + user accounts + user content
    # (strategies/models/clients/portfolios), so registrations survive redeploys.
    for db_rel in DBS:
        p = ROOT / db_rel
        if p.exists():
            shutil.copy2(p, STAGE / Path(db_rel).name)
        else:
            print(f"  !! {p} missing (skipped)")

    # Token-signing secret ships too, so sessions survive redeploys
    # (auth._get_secret pulls it back via remote_store when absent).
    secret = ROOT / "webapp" / ".secret_key"
    if secret.exists():
        dst = STAGE / "webapp" / ".secret_key"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(secret, dst)

    entries = []
    total = 0
    for p in sorted(STAGE.rglob("*")):
        if not p.is_file():
            continue
        size = p.stat().st_size
        total += size
        entries.append({
            "path": p.relative_to(STAGE).as_posix(),   # runtime path under data/
            "size": size,
            "sha256": sha256(p),
        })

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "schema": 1,
        "files": entries,
        "total_bytes": total,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    n = len(entries)
    big = sorted(entries, key=lambda e: -e["size"])[:8]
    print(f"staged {n} files, {total / 1e6:.1f} MB -> deploy/data/")
    for e in big:
        print(f"    {e['size'] / 1e6:8.1f} MB  {e['path']}")
    print(f"manifest: deploy/manifest.json ({MANIFEST.stat().st_size / 1e3:.0f} KB)")
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
