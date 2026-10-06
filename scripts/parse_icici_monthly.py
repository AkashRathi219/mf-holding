"""Parse ICICI Prudential monthly disclosure workbooks into the parsed store.

Same job contract as ``scripts/parse_window.py`` (batch_parser needs doc,
parsed_dir, amc_name, year, month, sha256), scoped to ICICI only.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import main as pipeline  # noqa: E402

RAW = BASE / "data/raw/pdfs/ICICI_Prudential_Mutual_Fund"
PARSED = BASE / "data/parsed/amc_websites/ICICI_Prudential_Mutual_Fund"
AMC = "ICICI Prudential Mutual Fund"
MONTHS = ("2026-06", "2026-07", "2026-08")
FORCE = "--force" in sys.argv


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    jobs: list[dict] = []
    need = 0
    for m in MONTHS:
        y, mm = m.split("-")
        mdir = RAW / y / mm
        if not mdir.is_dir():
            continue
        for doc in sorted(p for p in mdir.rglob("*")
                          if p.is_file() and p.suffix.lower() in (".xlsx", ".xls")):
            out = pipeline._parsed_json_path(doc, AMC, int(y), int(mm), PARSED)
            fresh = False
            if not FORCE:
                try:
                    fresh = pipeline._parse_cache_fresh(doc, out)
                except Exception:
                    pass
            jobs.append({"doc": str(doc), "parsed_dir": str(PARSED),
                         "amc_name": AMC, "year": int(y), "month": int(mm),
                         "sha256": sha256_of(doc), "force": FORCE})
            if not fresh:
                need += 1
    print(f"ICICI workbooks: {len(jobs)}   need parsing: {need}"
          f"{'   (forced)' if FORCE else ''}")

    from src.batch_parser import batch_parse
    res = batch_parse(jobs, workers=4)
    print(res["counts"], f"wall={res['wall_s']:.0f}s")

    # report what actually became loadable
    import json
    ok = bad = 0
    dates: dict[str, int] = {}
    for j in jobs:
        out = pipeline._parsed_json_path(
            Path(j["doc"]), AMC, j["year"], j["month"], PARSED)
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            bad += 1
            continue
        sch = d.get("schemes") or {}
        usable = [v for v in sch.values()
                  if isinstance(v, dict) and v.get("holdings")]
        if usable:
            ok += 1
            for v in usable:
                key = str(v.get("date") or "?")
                dates[key] = dates.get(key, 0) + 1
        else:
            bad += 1
    print(f"\nparsed files with loadable holdings: {ok}   without: {bad}")
    print("\nas_of labels found:")
    for k, v in sorted(dates.items(), key=lambda kv: -kv[1])[:12]:
        print(f"   {k:44s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())