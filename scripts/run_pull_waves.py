"""Wave coordinator for scripts/pull_annual_results.py.

Splits the remaining statement backfill into parallel batches and runs them
as background worker processes, then merges each worker's status sidecar
back into the master status file:

- universe: NIFTY 50 constituents (data/nifty/constituents/NIFTY_50.csv)
  intersected with the tracked-equity identity map;
- wave: `--agents` workers x `--batch` symbols each, started `--stagger`
  seconds apart so NSE rate-limiting warm-ups don't collide;
- each worker writes its own --status-path (the shared master file would be
  clobbered by concurrent whole-file rewrites);
- after a wave: worker statuses merge into the master sidecar, symbols that
  never reached `built` are retried on later waves up to --max-attempts, and
  the coverage report is refreshed when available.

Usage:
    python scripts/run_pull_waves.py --dry-run          # show the plan
    python scripts/run_pull_waves.py                    # all NIFTY 50 waves
    python scripts/run_pull_waves.py --agents 3 --batch 10 --waves 1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stock_identity import load_identity  # noqa: E402
from src.stock_common import load_json, save_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW_RESULTS_DIR = ROOT / "data" / "raw" / "financial_results"
MASTER_STATUS = RAW_RESULTS_DIR / "annual_fy25_26_status.json"
NIFTY50_CSV = ROOT / "data" / "nifty" / "constituents" / "NIFTY_50.csv"
LOG_DIR = ROOT / "logs" / "pull_waves"

# Mirror scripts/run_pull.ps1: GLM vision extraction billed straight through
# the OpenRouter API (the opencode free tier is retired for these runs).
DEFAULT_ENV = {
    "STMT_BACKEND": "openrouter",
    "STMT_AI": "1",
    "STMT_AI_MODEL": "z-ai/glm-5.3-flash",
    "STMT_AI_FALLBACK": "never",
    "PYTHONUTF8": "1",
}


def nifty50_universe() -> list[tuple[str, str]]:
    """[(isin, symbol)] for NIFTY 50 constituents that the app tracks."""
    ident = load_identity()
    out: list[tuple[str, str]] = []
    if not NIFTY50_CSV.exists():
        raise SystemExit(f"missing constituents CSV: {NIFTY50_CSV}")
    with open(NIFTY50_CSV, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            isin = (r.get("ISIN Code") or "").strip().upper()
            sym = (r.get("Symbol") or "").strip().upper()
            if isin and isin in ident and ident[isin].get("symbol"):
                out.append((isin, ident[isin]["symbol"].upper() or sym))
    return out


def remaining(universe: list[tuple[str, str]], status: dict,
              max_attempts: int) -> list[tuple[str, str]]:
    out = []
    for isin, sym in universe:
        st = status.get(isin) or {}
        if st.get("status") == "built":
            continue
        if st.get("attempts", 0) >= max_attempts:
            continue
        out.append((isin, sym))
    return out


def merge_status(master_path: Path, worker_paths: list[Path]) -> dict:
    master = load_json(master_path) or {}
    for wp in worker_paths:
        for isin, st in (load_json(wp) or {}).items():
            prev = master.get(isin) or {}
            # attempts were already incremented pre-launch; the worker's
            # status row (latest outcome) simply replaces the old one
            st["attempts"] = prev.get("attempts", 0)
            master[isin] = st
    save_json(master_path, master)
    return master


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=5,
                    help="parallel worker processes per wave")
    ap.add_argument("--batch", type=int, default=10,
                    help="symbols per worker per wave")
    ap.add_argument("--waves", type=int, default=0,
                    help="stop after N waves (0 = until universe done)")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="drop a symbol from the plan after this many "
                         "non-built attempts")
    ap.add_argument("--stagger", type=float, default=45.0,
                    help="seconds between worker starts (NSE warm-up)")
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--max-docs", type=int, default=90)
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--ai-pages", type=int, default=24)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--from-local", action="store_true",
                    help="parse from local feed snapshots + downloaded "
                         "PDFs (scripts/download_results.py output); "
                         "no NSE calls")
    ap.add_argument("--refresh", action="store_true",
                    help="rebuild every universe symbol (passes "
                         "--refresh-status to workers) instead of only "
                         "symbols not yet built")
    ap.add_argument("--symbols-file", default="",
                    help="override universe with a text file, one SYMBOL per "
                         "line (resolved through identity)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.symbols_file:
        want = [s.strip().upper() for s in
                Path(args.symbols_file).read_text(encoding="utf-8")
                .splitlines() if s.strip()]
        ident = load_identity()
        by_sym = {v.get("symbol", "").upper(): k for k, v in ident.items()
                  if v.get("symbol")}
        universe = [(by_sym[s], s) for s in want if s in by_sym]
    else:
        universe = nifty50_universe()

    for k, v in DEFAULT_ENV.items():
        os.environ.setdefault(k, v)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    status = load_json(MASTER_STATUS) or {}
    wave_no = 0
    totals: dict[str, int] = {}
    while True:
        todo = list(universe) if args.refresh \
            else remaining(universe, status, args.max_attempts)
        if not todo:
            break
        wave_no += 1
        chunk = todo[:args.agents * args.batch]
        groups = [chunk[i:i + args.batch]
                  for i in range(0, len(chunk), args.batch)]
        print(f"\n=== wave {wave_no}: {len(chunk)} symbols across "
              f"{len(groups)} workers ({len(todo)} remaining in universe) "
              f"===", flush=True)
        for i, g in enumerate(groups, 1):
            print(f"  worker {i}: " + ", ".join(s for _i, s in g), flush=True)
        if args.dry_run:
            break

        procs: list[tuple[int, subprocess.Popen, Path]] = []
        for i, g in enumerate(groups, 1):
            symbols = ",".join(s for _i, s in g)
            wstatus = RAW_RESULTS_DIR / f"_status_wave{wave_no:02d}_{i}.json"
            if wstatus.exists():
                wstatus.unlink()
            for isin, _s in g:
                st = status.setdefault(isin, {})
                st["attempts"] = st.get("attempts", 0) + 1
            save_json(MASTER_STATUS, status)
            out_log = LOG_DIR / (f"wave{wave_no:02d}_{i}_"
                                 f"{g[0][1]}.out.log")
            err_log = LOG_DIR / (f"wave{wave_no:02d}_{i}_"
                                 f"{g[0][1]}.err.log")
            run_args = [sys.executable, "-u", "-X", "utf8",
                        str(ROOT / "scripts" / "pull_annual_results.py"),
                        "--symbols", symbols,
                        "--status-path", str(wstatus),
                        "--sleep", str(args.sleep),
                        "--max-docs", str(args.max_docs),
                        "--pages", str(args.pages),
                        "--ai-pages", str(args.ai_pages)]
            if args.no_ai:
                run_args.append("--no-ai")
            if args.refresh:
                run_args.append("--refresh-status")
            if args.from_local:
                run_args.append("--from-local")
            with open(out_log, "w", encoding="utf-8") as fo, \
                    open(err_log, "w", encoding="utf-8") as fe:
                p = subprocess.Popen(run_args, cwd=str(ROOT), stdout=fo,
                                     stderr=fe)
            procs.append((i, p, wstatus))
            print(f"  worker {i} PID {p.pid} -> {out_log.name}", flush=True)
            if i < len(groups):
                time.sleep(args.stagger)

        for i, p, _w in procs:
            rc = p.wait()
            print(f"  worker {i} exited rc={rc}", flush=True)

        status = merge_status(MASTER_STATUS, [w for _i, _p, w in procs])
        built = sum(1 for isin, _s in chunk
                    if (status.get(isin) or {}).get("status") == "built")
        totals[f"wave{wave_no}"] = built
        print(f"  wave {wave_no} built {built}/{len(chunk)}", flush=True)
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "statement_coverage_report",
                ROOT / "scripts" / "statement_coverage_report.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.refresh_outputs()
            print("  coverage report refreshed", flush=True)
        except Exception as e:  # coverage is a nice-to-have, never fatal
            print(f"  (coverage refresh skipped: {e})", flush=True)
        if args.waves and wave_no >= args.waves:
            break

    built_all = sum(1 for isin, _s in universe
                    if (load_json(MASTER_STATUS) or {}).get(isin, {})
                    .get("status") == "built")
    print(f"\ndone: {built_all}/{len(universe)} universe stocks built "
          f"({json.dumps(totals)}) "
          f"at {datetime.now().strftime('%d-%b-%Y %H:%M')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
