"""Wave 3: targeted refresh of built-but-incomplete stocks.

Waits for the wave-2 coordinator to exit, then re-runs the thin builds
(zero/missing consolidated annual rows, missing FY25/FY26, sparse quarters)
through the GLM vision tier with --refresh-status. PDFs and per-page AI
payloads are cached, so only the failed pages cost anything.

Usage:
    python scripts/run_refresh_wave3.py --wait-pid 3444
    python scripts/run_refresh_wave3.py --now
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stock_common import load_json, save_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW_RESULTS_DIR = ROOT / "data" / "raw" / "financial_results"
MASTER_STATUS = RAW_RESULTS_DIR / "annual_fy25_26_status.json"
LOG_DIR = ROOT / "logs" / "pull_waves"

# Mirror the wave runner env: free opencode vision backend, never paid.
ENV = {
    "STMT_BACKEND": "opencode",
    "STMT_AI": "1",
    "OPCODE_MODEL": "opencode-go/glm-5.3-flash",
    "STMT_AI_FALLBACK": "never",
    "PYTHONUTF8": "1",
}

# Disjoint worker groups (30-Aug night run): G1 missing recent FYs + TCS,
# G2/G3 zero consolidated annual rows, G4 sparse. KRBL already re-built.
GROUPS = [
    ["RELIANCE", "SBIN", "AXISBANK", "DRREDDY", "SJVN", "BEL", "TCS"],
    ["INFY", "TATASTEEL", "TITAN", "JSWSTEEL", "NESTLEIND", "ONGC",
     "HINDALCO"],
    ["GRASIM", "BHARTIARTL", "ASIANPAINT", "BAJAJFINSV", "SHRIRAMFIN",
     "TRENT", "CIPLA"],
    ["COALINDIA", "EICHERMOT", "HDFCBANK", "KOTAKBANK", "BAJFINANCE",
     "JIOFIN"],
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-pid", type=int, default=0,
                    help="block until this PID exits (the wave-2 "
                         "coordinator); 0 = start immediately")
    ap.add_argument("--stagger", type=float, default=45.0)
    args = ap.parse_args()

    if args.wait_pid:
        print(f"waiting for wave-2 coordinator PID {args.wait_pid}...",
              flush=True)
        # NOTE: os.kill(pid, 0) TERMINATES the process on Windows
        # (TerminateProcess) — probe liveness via tasklist instead.
        while True:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {args.wait_pid}"],
                               capture_output=True, text=True)
            if str(args.wait_pid) not in (r.stdout or ""):
                break
            time.sleep(30)
        print(f"coordinator exited at "
              f"{datetime.now().strftime('%d-%b-%Y %H:%M')}", flush=True)

    for k, v in ENV.items():
        os.environ[k] = v
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    procs: list[tuple[int, subprocess.Popen, Path]] = []
    for i, group in enumerate(GROUPS, 1):
        sidecar = RAW_RESULTS_DIR / f"_status_wave03b_{i}.json"
        if sidecar.exists():
            sidecar.unlink()
        out_log = LOG_DIR / f"wave03_{i}_{group[0]}.out.log"
        err_log = LOG_DIR / f"wave03_{i}_{group[0]}.err.log"
        run_args = [sys.executable, "-u", "-X", "utf8",
                    str(ROOT / "scripts" / "pull_annual_results.py"),
                    "--symbols", ",".join(group),
                    "--refresh-status",
                    "--status-path", str(sidecar),
                    "--sleep", "1.2", "--max-docs", "90",
                    "--pages", "30", "--ai-pages", "24"]
        with open(out_log, "w", encoding="utf-8") as fo, \
                open(err_log, "w", encoding="utf-8") as fe:
            p = subprocess.Popen(run_args, cwd=str(ROOT), stdout=fo,
                                 stderr=fe, env=os.environ.copy())
        procs.append((i, p, sidecar))
        print(f"worker {i} PID {p.pid}: {', '.join(group)}",
              flush=True)
        if i < len(GROUPS):
            time.sleep(args.stagger)

    for i, p, _s in procs:
        rc = p.wait()
        print(f"worker {i} exited rc={rc}", flush=True)

    # Merge worker sidecars into the master file (status rows replace;
    # attempts kept from the previous record like the wave runner does).
    master = load_json(MASTER_STATUS) or {}
    built = 0
    for _i, _p, sidecar in procs:
        for isin, st in (load_json(sidecar) or {}).items():
            st["attempts"] = (master.get(isin) or {}).get("attempts", 0)
            master[isin] = st
            if st.get("status") == "built":
                built += 1
    save_json(MASTER_STATUS, master)
    print(f"merged: {built}/{sum(len(g) for g in GROUPS)} refreshed stocks "
          f"built at {datetime.now().strftime('%d-%b-%Y %H:%M')}", flush=True)

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "statement_coverage_report",
            ROOT / "scripts" / "statement_coverage_report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.refresh_outputs()
        print("coverage report refreshed", flush=True)
    except Exception as e:  # coverage is a nice-to-have, never fatal
        print(f"(coverage refresh skipped: {e})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
