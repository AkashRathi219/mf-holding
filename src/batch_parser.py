"""File-level parallel batch parsing.

The agent network inside ``src.pdf_agents`` parallelises sections WITHIN one
PDF, but OCR-fallback documents bypass it and batch runs in ``main.py`` were
sequential across files. This module adds the missing outer layer: a
``ProcessPoolExecutor`` over whole documents so OCR-heavy AMCs (e.g. ICICI's
outline-rendered factsheets at ~6-15s each) use every core.

Each job is fully described by a picklable dict::

    {"doc": str, "amc_name": str, "year": int, "month": int,
     "parsed_dir": str, "sha256": str}

The worker re-imports the pipeline helpers from ``main`` (cheap once per
process), applies the same sha256 parse-cache rules as the sequential path,
stamps ``metadata.source_sha256``, saves via ``save_parsed``, and returns a
small result dict (never raises).
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _worker_preamble() -> None:
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _parse_one(job: dict) -> dict:
    """Parse one document inside a worker process. Never raises."""
    t0 = time.perf_counter()
    out = {"doc": job["doc"], "status": "parsed", "seconds": 0.0, "schemes": []}
    try:
        _worker_preamble()
        import main as pipeline  # pipeline helpers (parse_document / save_parsed)
        from src.pdf_segregator import parse_grouped_pdf  # noqa: F401 (parity)

        doc = Path(job["doc"])
        parsed_dir = Path(job["parsed_dir"])

        out_json = pipeline._parsed_json_path(
            doc, job["amc_name"], job["year"], job["month"], parsed_dir)
        if not job.get("force") and pipeline._parse_cache_fresh(doc, out_json):
            try:
                cached = __import__("json").loads(out_json.read_text(encoding="utf-8"))
                out["schemes"] = pipeline._extract_scheme_names(cached)
            except Exception:
                pass
            out["status"] = "cached"
            return out

        parsed = pipeline.parse_document(doc)
        parsed["amc_name"] = job["amc_name"]
        parsed["fetch_month"] = job["month"]
        parsed["fetch_year"] = job["year"]
        meta = parsed.setdefault("metadata", {})
        meta["source_sha256"] = job["sha256"]

        # Cheap quality counters for telemetry.
        n_rows = len(parsed.get("equity_holdings") or []) \
            + len(parsed.get("debt_holdings") or [])
        if meta.get("ocr"):
            out["rows"] = sum(len(t) if isinstance(t, list) else 1
                              for t in (parsed.get("equity_holdings") or []))
        else:
            out["rows"] = n_rows

        out["schemes"] = pipeline._extract_scheme_names(parsed)
        pipeline.save_parsed(doc, parsed, job["amc_name"],
                             job["year"], job["month"], parsed_dir)
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:300]
    finally:
        out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def batch_parse(jobs: list[dict], workers: int | None = None) -> dict:
    """Run parse jobs across a process pool.

    Returns {"items": [per-job dicts in input order], "wall_s": float,
             "counts": {parsed, cached, failed}}."""
    t0 = time.perf_counter()
    items: list[dict | None] = [None] * len(jobs)
    if not jobs:
        return {"items": [], "wall_s": 0.0,
                "counts": {"parsed": 0, "cached": 0, "failed": 0}}

    if workers is None or int(workers) <= 0:
        import os
        workers = min(os.cpu_count() or 2, 8)
    workers = max(1, min(int(workers), len(jobs)))

    counts = {"parsed": 0, "cached": 0, "failed": 0}
    if workers == 1:
        for i, job in enumerate(jobs):
            res = _parse_one(job)
            items[i] = res
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers,
                                     initializer=_worker_preamble) as pool:
                future_map = {pool.submit(_parse_one, j): i
                              for i, j in enumerate(jobs)}
                for fut in as_completed(future_map):
                    i = future_map[fut]
                    try:
                        items[i] = fut.result()
                    except Exception as e:  # worker died hard
                        items[i] = {"doc": jobs[i]["doc"], "status": "error",
                                    "error": str(e)[:300], "seconds": 0.0,
                                    "schemes": []}
        except (OSError, ImportError, RuntimeError) as e:
            logger.warning(f"Process pool unavailable ({e}); parsing sequentially")
            for i, job in enumerate(jobs):
                items[i] = _parse_one(job)

    for i, r in enumerate(items):
        if r is None:  # defensive; should not happen
            items[i] = {"doc": jobs[i]["doc"], "status": "error",
                        "error": "no result", "seconds": 0.0, "schemes": []}
            r = items[i]
        counts[r["status"] if r["status"] in ("parsed", "cached") else "failed"] += 1
    wall = round(time.perf_counter() - t0, 1)
    logger.info(f"Batch parse: {counts} in {wall}s "
                f"(workers={workers}, docs={len(jobs)})")
    return {"items": items, "wall_s": wall, "counts": counts}
