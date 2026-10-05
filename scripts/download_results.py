"""Download-first phase for the NSE result-statement backfill.

NSE rate-limits aggressively (PDF refusals + feed throttling after a few
hundred calls), so network I/O and CPU-bound parsing are split:

1. THIS script (network phase, parallel): for every symbol, pull the
   corporate-announcements result feed, download every result PDF into
   data/raw/financial_results/<SYMBOL>/ and snapshot the feed to
   _announcements/<SYMBOL>.json — headline, filing class (audited/unaudited),
   declared period, local path, download status. Several retry passes go
   after refusals. NO parsing happens here.

2. Parsing phase (offline): scripts/pull_annual_results.py --from-local
   reads the snapshots and parses the local PDFs with zero NSE calls.

Usage:
    python scripts/download_results.py --dry-run
    python scripts/download_results.py --workers 5            # NIFTY 50
    python scripts/download_results.py --symbols SBIN,TCS
"""
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.financial_statements import download_pdf  # noqa: E402
from src.stock_common import save_json  # noqa: E402
from src.stock_identity import load_identity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pull_annual_results import (  # noqa: E402  (scripts dir on path)
    _audit_of, _fetch_result_announcements, _year_hint)

RAW_RESULTS_DIR = ROOT / "data" / "raw" / "financial_results"
ANNOUNCE_DIR = RAW_RESULTS_DIR / "_announcements"
DL_STATUS_PATH = RAW_RESULTS_DIR / "download_status.json"
NIFTY50_CSV = ROOT / "data" / "nifty" / "constituents" / "NIFTY_50.csv"


def nifty50_universe() -> list[tuple[str, str]]:
    ident = load_identity()
    out: list[tuple[str, str]] = []
    with open(NIFTY50_CSV, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            isin = (r.get("ISIN Code") or "").strip().upper()
            if isin and isin in ident and ident[isin].get("symbol"):
                out.append((isin, ident[isin]["symbol"].upper()))
    return out


def snapshot_path(symbol: str) -> Path:
    return ANNOUNCE_DIR / f"{symbol}.json"


def download_one(task: tuple[str, str, int, int]) -> dict:
    """Worker: feed scan + PDF download for one symbol. No parsing."""
    isin, symbol, pages, max_docs = task
    anns = _fetch_result_announcements(symbol, pages=pages)[:max_docs]
    entries: list[dict] = []
    ok = 0
    for a in anns:
        audit = _audit_of(a["headline"])
        entry = {"date": a["date"], "headline": a["headline"][:300],
                 "url": a["url"], "audit": audit,
                 "period": _year_hint(a["headline"]),
                 "path": None, "size": None, "downloaded": False}
        pdf = download_pdf(a["url"], symbol)
        if pdf:
            entry["path"] = str(pdf)
            entry["size"] = pdf.stat().st_size
            entry["downloaded"] = True
            ok += 1
        entries.append(entry)
    ANNOUNCE_DIR.mkdir(parents=True, exist_ok=True)
    save_json(snapshot_path(symbol), {
        "isin": isin, "symbol": symbol,
        "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "pages_scanned": pages,
        "announcements": entries,
    })
    missing = len(entries) - ok
    status = ("no_result_announcements" if not entries
              else "downloaded" if missing == 0 else "partial")
    print(f"[{symbol}] {len(entries)} results, {ok} downloaded, "
          f"{missing} refused", flush=True)
    return {"isin": isin, "symbol": symbol, "status": status,
            "total": len(entries), "downloaded": ok, "missing": missing}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--max-docs", type=int, default=90)
    ap.add_argument("--passes", type=int, default=3,
                    help="retry passes over refused downloads")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=90.0,
                    help="seconds between passes (throttle cool-down)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        ident = load_identity()
        by_sym = {v.get("symbol", "").upper(): k for k, v in ident.items()
                  if v.get("symbol")}
        universe = [(by_sym[s], s) for s in sorted(want) if s in by_sym]
    else:
        universe = nifty50_universe()
    if args.limit:
        universe = universe[:args.limit]
    print(f"universe: {len(universe)} symbols · workers {args.workers} · "
          f"passes {args.passes}", flush=True)
    if args.dry_run:
        for isin, sym in universe:
            print(f"  {sym} {isin}")
        return 0

    dl_status = json.loads(DL_STATUS_PATH.read_text(encoding="utf-8")) \
        if DL_STATUS_PATH.exists() else {}
    ctx = mp.get_context("spawn")
    tasks = [(isin, sym, args.pages, args.max_docs) for isin, sym in universe]
    for pass_no in range(1, args.passes + 1):
        # pass 1: everything; later passes: only symbols with refusals
        if pass_no == 1:
            todo = tasks
        else:
            todo = [t for t in tasks
                    if (dl_status.get(t[0]) or {}).get("missing", 1) > 0
                    and (dl_status.get(t[0]) or {}).get("status")
                    not in ("no_result_announcements",)]
            if not todo:
                print(f"pass {pass_no}: nothing to retry — done", flush=True)
                break
            print(f"pass {pass_no}: retrying {len(todo)} symbols with "
                  f"refused PDFs (pausing {args.pause:.0f}s first)",
                  flush=True)
            time.sleep(args.pause)
        with ctx.Pool(args.workers) as pool:
            for res in pool.imap_unordered(download_one, todo):
                dl_status[res["isin"]] = {**res, "pass": pass_no,
                                          "at": datetime.now()
                                          .strftime("%Y-%m-%dT%H:%M:%S")}
                save_json(DL_STATUS_PATH, dl_status)
        done = sum(1 for _i, s in universe
                   if (dl_status.get(_i) or {}).get("status") == "downloaded")
        print(f"pass {pass_no} complete: {done}/{len(universe)} symbols "
              f"fully downloaded", flush=True)

    tot_dl = sum((dl_status.get(i) or {}).get("downloaded", 0)
                 for i, _s in universe)
    print(f"\nfinal: {tot_dl} PDFs on disk across {len(universe)} symbols "
          f"at {datetime.now().strftime('%d-%b-%Y %H:%M')}", flush=True)
    print("next: python scripts/pull_annual_results.py --from-local",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
