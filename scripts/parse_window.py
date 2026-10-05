"""Build parse jobs for the trailing-3-month window and report the backlog."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

RAW = BASE / "data" / "raw" / "pdfs"
PARSED = BASE / "data" / "parsed" / "amc_websites"
MONTHS = ("2026-07", "2026-08", "2026-09")
ALL_WINDOW = ("2026-06", "2026-07", "2026-08", "2026-09")


def amc_name_from_dir(d: Path) -> str:
    return d.name.replace("_", " ")


def build_jobs(months=MONTHS) -> tuple[list[dict], int]:
    import main as pipeline

    import hashlib

    jobs: list[dict] = []
    need_parse = 0
    for m in months:
        year, mon = m.split("-")
        for amc_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
            mdir = amc_dir / year / mon
            if not mdir.exists():
                continue
            for doc in sorted(p for p in mdir.rglob("*") if p.is_file()):
                out_json = pipeline._parsed_json_path(
                    doc, amc_name_from_dir(amc_dir), int(year), int(mon), PARSED)
                fresh = False
                try:
                    fresh = pipeline._parse_cache_fresh(doc, out_json)
                except Exception:
                    fresh = False
                h = hashlib.sha256()
                with open(doc, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                jobs.append({
                    "doc": str(doc),
                    "parsed_dir": str(PARSED),
                    "amc_name": amc_name_from_dir(amc_dir),
                    "year": int(year),
                    "month": int(mon),
                    # batch_parser stamps this into metadata.source_sha256
                    "sha256": h.hexdigest(),
                    "force": False,
                })
                if not fresh:
                    need_parse += 1
    return jobs, need_parse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--execute", action="store_true",
                    help="actually parse (default: dry report only)")
    ap.add_argument("--months", default=",".join(MONTHS),
                    help="comma-separated YYYY-MM list")
    args = ap.parse_args()

    months = tuple(m.strip() for m in args.months.split(",") if m.strip())
    jobs, need = build_jobs(months)
    print(f"docs in window : {len(jobs)}")
    print(f"need parsing   : {need}   (already cached: {len(jobs) - need})")
    if not args.execute:
        return 0
    if args.limit:
        jobs = jobs[: args.limit]

    from src.batch_parser import batch_parse

    res = batch_parse(jobs, workers=args.workers)
    print(json.dumps(res.get("counts"), indent=2))
    print(f"wall_s: {res.get('wall_s'):.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())