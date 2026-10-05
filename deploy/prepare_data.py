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

import fnmatch
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
    "stock_financials",  # stocks : parsed quarterly/annual statements per ISIN
    "stocks",            # stocks : identity.json (ISIN -> NSE symbol)
    "reference",         # bonds/bonds_catalog.json + isin_latest_nav.json + misc
    "nifty",             # nifty/weights.json (index fund weights)
    "universe",          # navall.txt + Combined NAV CSV (DB seed inputs)
    "parsed",            # amfi/amc_websites/advisorkhoj holdings JSONs
]

# Files under data/ that are needed but live outside the dirs above.
RUNTIME_FILES = [
    "data/feedback.json",
    "data/logs/refresh_state.json",  # superadmin last-fetched rollup (tiny)
]

# Extra root-level files (repo root, not under data/).
ROOT_FILES = [
    "CAS_sample_portfolio_holdings.json",
]

# data/ content known to be pipeline-only clutter -- never staged.
SKIP_DIRS = {"raw", "stock_bhavcopy", "pdfs", "logs", "downloads", ".staging"}

DBS = ["data/webapp.db", "data/userdata.db", "data/webapp_auth.db"]

# --- upload policy: JSON, plus the few non-JSON files the webapp opens itself ---
#
# The app's loaders glob only ``*.json`` out of the parsed stores and the
# per-scheme/per-ISIN dirs. The remaining non-JSON files in the staged tree are
# pipeline-side leftovers that no module under ``webapp/`` ever reads: the
# per-document CSVs under ``parsed/``, the Nifty index constituent PDFs/XLSX,
# and the NSE bond raw dumps (the runtime artifact is ``bonds_catalog.json``).
#
# Those stay out of the bucket. Source PDFs in particular are routed
# separately and are never staged (see SKIP_DIRS), so a staging run cannot
# quietly upload the document corpus.
RUNTIME_NONJSON = (
    "webapp.db",                      # the frontend database itself
    "userdata.db",                    # user strategies/models/clients/portfolios
    "webapp_auth.db",                 # accounts + password hashes
    "webapp/.secret_key",             # token-signing secret (sessions)
    "reference/equity_isin.db",
    "universe/navall.txt",            # db.py NAVALL_TXT
    "universe/Combined NAV*.csv",     # db.py UNIVERSE_CSV
    "reference/equity_isins.csv",     # db.py EQUITY_ISINS_CSV
    "reference/discovery_needed.csv",  # db.py DISCOVERY_NEEDED_CSV
    "reference/no_disclosure.csv",    # db.py NO_DISCLOSURE_CSV
    "reference/discontinued_schemes.csv",  # db.py DISCONTINUED_CSV
    "reference/ter_*.csv",            # sole TER source
    "data/logs/refresh_state.json",
)


def prune_stage(keep_nonjson: tuple[str, ...]) -> tuple[int, int]:
    """Delete every staged file that is neither JSON nor explicitly required.

    Runs after copying and before the manifest is written, so the bucket keys,
    the staged tree and the manifest all describe the same set. Returns
    ``(files_removed, bytes_removed)``.
    """
    removed_n = removed_b = 0
    for p in sorted(STAGE.rglob("*"), reverse=True):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".json":
            continue
        rel = p.relative_to(STAGE).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in keep_nonjson):
            continue
        removed_b += p.stat().st_size
        p.unlink()
        removed_n += 1
    for d in sorted((p for p in STAGE.rglob("*") if p.is_dir()), reverse=True):
        try:
            next(d.iterdir())
        except StopIteration:
            d.rmdir()
        except OSError:
            pass
    return removed_n, removed_b


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

    # Latest NSE bond raw dumps (~2 MB) so the container can rebuild the
    # bond catalog immediately instead of waiting for its first fetch.
    raw_root = ROOT / "data" / "bond_market" / "raw"
    if raw_root.is_dir():
        dated = sorted((p for p in raw_root.iterdir() if p.is_dir()),
                       key=lambda p: p.name, reverse=True)
        for day_dir in dated:
            csvs = [p for p in day_dir.glob("*.csv") if p.stat().st_size > 0]
            if not csvs:
                continue
            for p in csvs:
                rel = p.relative_to(raw_root)
                dst = STAGE / "bond_market" / "raw" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
            print(f"  staged bond raw dumps: {day_dir.name} ({len(csvs)} files)")
            break

    dropped_n, dropped_b = prune_stage(RUNTIME_NONJSON)
    print(f"  pruned {dropped_n} non-runtime files ({dropped_b / 1e6:.1f} MB); "
          f"bucket = JSON + {len(RUNTIME_NONJSON)} required paths")

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
