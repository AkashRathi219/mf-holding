"""Flatten the doubled <AMC>/<AMC>/ segment left by the first gap-fetch run.

DocumentDownloader derives "<amc>/<year>/<month>" itself. The first run also
passed a per-AMC output_dir, so those files landed one level too deep. This
moves them up into the canonical layout WITHOUT clobbering anything that is
already in place (existing files always win).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw" / "pdfs"


def main() -> int:
    if not RAW.exists():
        print(f"missing {RAW}")
        return 1

    moved = skipped = 0
    for amc_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
        inner = amc_dir / amc_dir.name
        if not inner.is_dir():
            continue

        print(f"flattening {amc_dir.name}")
        for src_dir in sorted(p for p in inner.iterdir() if p.is_dir()):
            dst_dir = amc_dir / src_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src in sorted(p for p in src_dir.rglob("*") if p.is_file()):
                rel = src.relative_to(src_dir)
                dst = dst_dir / rel
                if dst.exists():
                    skipped += 1
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1

        # drop the emptied inner tree
        shutil.rmtree(inner, ignore_errors=True)

    print(f"\nmoved={moved} skipped_existing={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())