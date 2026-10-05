"""Fetch the missing mutual-fund disclosure months, resumable + batched.

For each (AMC, target month) that lacks raw documents:
  discover via the AMC's own adapter (production bindings) -> download -> log.

Usage:
    python scripts/fetch_gap_months.py --month 2026-08 [--amc NAME ...] [--dry-run]

Safe to re-run: an AMC/month with files already on disk is skipped unless
--force. Progress and per-AMC outcomes go to
``logs/gap_fetch_<month>.log`` so a long run can be polled while it works.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

LOG_DIR = BASE / "logs"
RAW = BASE / "data" / "raw" / "pdfs"


def safe_amc(name: str) -> str:
    return name.replace(" ", "_").replace("/", "-")


def has_month(amc_name: str, month: str) -> bool:
    d = RAW / safe_amc(amc_name) / month[:4] / month[5:7]
    return d.exists() and any(d.iterdir())


def load_registry() -> list[str]:
    reg = json.loads((BASE / "config" / "amc_registry.json").read_text(encoding="utf-8"))
    return [e["mf_name"] for e in reg if e.get("mf_name")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--amc", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--batch", type=int, default=6, help="AMCs per log line")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("gap")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_DIR / f"gap_fetch_{args.month}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)

    names = load_registry()
    if args.amc:
        names = [n for n in names if any(a.lower() in n.lower() for a in args.amc)]
    targets = [n for n in names if args.force or not has_month(n, args.month)]
    log.info("month=%s registry=%d targets=%d dry_run=%s",
             args.month, len(names), len(targets), args.dry_run)
    print(f"targets: {len(targets)} of {len(names)}", flush=True)

    from src.agents.production import build_discover, build_download

    done = failed = 0
    t0 = time.time()
    for i, amc in enumerate(targets, 1):
        try:
            year, mon = int(args.month[:4]), int(args.month[5:7])
            discover = build_discover(month=mon, year=year)
            links = discover("fast_http", amc) or []
            if args.dry_run:
                log.info("[%d/%d] %s DISCOVERED %d", i, len(targets), amc, len(links))
                done += 1
                continue
            # output_dir is REQUIRED: without it build_download is a deliberate
            # fail-closed no-op that returns 0 and writes nothing.
            # Pass the raw ROOT, not the per-AMC folder: DocumentDownloader
            # appends "<amc>/<year>/<month>" itself, so passing the AMC
            # folder here yields a doubled path segment.
            download = build_download(output_dir=RAW, month=mon, year=year)
            got = download(links) if links else 0
            log.info("[%d/%d] %s discovered=%d downloaded=%d", i, len(targets), amc, len(links), got)
            done += 1
        except Exception as exc:  # one AMC must never kill the batch
            log.warning("[%d/%d] %s FAILED %s: %s", i, len(targets), amc,
                        type(exc).__name__, str(exc)[:160])
            failed += 1
        if i % args.batch == 0:
            log.info("--- progress %d/%d ok=%d failed=%d elapsed=%.0fs",
                     i, len(targets), done, failed, time.time() - t0)
            print(f"  {i}/{len(targets)} ok={done} failed={failed} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    log.info("DONE month=%s ok=%d failed=%d elapsed=%.0fs",
             args.month, done, failed, time.time() - t0)
    print(f"DONE ok={done} failed={failed} elapsed={time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())