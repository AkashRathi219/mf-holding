"""[Phase 4 sweep] Force re-parse office documents (the Derivative* disclosure
sheets live in XLSX/ZIP), then backfill any document still missing a parsed
output (e.g. the HSBC pool-crash gap). Sequential single process — no parse
pool, deterministic, resume-safe (every doc is sha256-cache-checked).

Run:  .venv-slim/Scripts/python.exe scripts/sweep_force_office.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import (  # noqa: E402
    BASE_DIR, _parse_cache_fresh, _parse_single_doc, _parsed_json_path,
    _setup_from_config, load_config,
)

OFFICE = {".xlsx", ".xls", ".zip", ".xlsb", ".csv"}


def main() -> None:
    _setup_from_config()
    config = load_config()
    pdfs_dir = BASE_DIR / config["paths"]["pdfs_dir"]
    parsed_dir = BASE_DIR / config["paths"]["parsed_dir"]

    def log(msg: str) -> None:
        print(msg, flush=True)

    done = skipped = failed = 0
    t0 = time.time()
    total_amcs = sum(1 for d in pdfs_dir.iterdir() if d.is_dir())
    for idx, amc_dir in enumerate(sorted(pdfs_dir.iterdir()), 1):
        if not amc_dir.is_dir():
            continue
        amc_name = amc_dir.name.replace("_", " ")
        for ydir in sorted(amc_dir.iterdir()):
            if not ydir.is_dir() or not ydir.name.isdigit():
                continue
            y = int(ydir.name)
            for mdir in sorted(ydir.iterdir()):
                if not mdir.is_dir() or not mdir.name.isdigit():
                    continue
                m = int(mdir.name)
                for doc in sorted(mdir.iterdir()):
                    if not doc.is_file():
                        continue
                    office = doc.suffix.lower() in OFFICE
                    if not office:
                        out_json = _parsed_json_path(doc, amc_name, y, m,
                                                     parsed_dir)
                        if _parse_cache_fresh(doc, out_json):
                            skipped += 1
                            continue
                    if _parse_single_doc(doc, amc_name, y, m, parsed_dir,
                                         force=office):
                        done += 1
                    else:
                        failed += 1
        log(f"[{idx}/{total_amcs}] {amc_dir.name}: parsed={done} "
            f"skipped={skipped} failed={failed} ({time.time()-t0:.0f}s)")
    log(f"SWEEP DONE parsed={done} skipped={skipped} failed={failed} "
        f"wall={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
