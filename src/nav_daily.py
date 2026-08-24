"""Daily NAV refresh agent.

Appends the latest NAV points (from AMFI's NAV-history report) to the per-scheme
history files under ``data/nav_history/`` so the webapp's NAV charts stay current.

Runs as a daily scheduled job (or manually)::

    python -m src.nav_daily           # refresh the last 10 days (default)
    python -m src.nav_daily --days 5  # refresh the last 5 days

How it works
------------
* One AMFI call returns the NAV for *every* scheme over a short recent window
  (``DownloadNAVHistoryReport_Po.aspx``, same endpoint the full backfill uses).
* Each scheme's new ``(date, nav)`` points are merged into its existing
  ``data/nav_history/<code>.json`` file (de-duplicated by date, kept in
  chronological order).
* A scheme with NO file yet is never seeded with a thin recent-window stub —
  that stub would shadow the full R2 history forever and starve analytics.
  Missing files are filled with FULL histories (R2 object, then the AMFI
  portal walk under a per-run cap); codes that can't be filled are left
  absent so the read path fetches the full file on demand.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .nav_history import _date_key, _fetch_amfi, _parse_nav_text, load_universe

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "nav_history"

# A thin "recent-window-only" file is worse than no file: webapp.remote_store
# treats an existing local file as authoritative and never re-fetches, so a
# stub permanently shadows the full R2 history and analytics honestly reports
# "not enough NAV history" for years. Missing files must be filled with FULL
# histories only (R2 object first, then the AMFI portal walk), never seeded
# from the short daily window. Cap the per-run full-fetch fan-out so one cold
# start can't hammer the portal (the rest are picked up on subsequent runs or
# served straight from R2 on first read).
# [DATA-POLICY: AMFI/AMC/NSE only — the third-party mirror is retired.]
MAX_FULL_HISTORY_FETCHES_PER_RUN = 100
PORTAL_WALK_DELAY = 1.0  # seconds between AMFI portal window requests


def _load_history(path: Path) -> list[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc, doc.get("history") or []
    except Exception:
        return {}, []


def update_latest_navs(days: int = 10, out_dir: Path = OUT_DIR) -> dict:
    """Fetch the last ``days`` days of AMFI NAVs and merge into nav_history files.

    Returns a summary dict. Raises on network/AMFI failure so the caller (the
    scheduler) can log and retry next cycle. Telemetry is written to
    data/logs/refresh_log.jsonl (src.refresh_log).
    """
    from .refresh_log import track
    with track("nav_daily", days=days) as _meta:
        summary = _update_latest_navs_impl(days, out_dir)
        _meta.update(summary)
        return summary


def _full_history_doc(code: str, out_dir: Path = OUT_DIR) -> dict | None:
    """Full since-inception daily history for one code — AMFI portal only
    [DATA-POLICY: AMFI/AMC/NSE only; the third-party mirror is retired]."""
    from .nav_history import fetch_codes_history
    summary = fetch_codes_history([code], out_dir=out_dir)
    path = out_dir / f"{code}.json"
    if summary.get("written") and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _update_latest_navs_impl(days: int = 10, out_dir: Path = OUT_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    end = date.today()
    start = end - timedelta(days=days)

    text = _fetch_amfi(start, end)
    rows = _parse_nav_text(text)
    by_code: dict[str, list[tuple]] = {}
    for code, datestr, nav, name, plan, option, isin, isin_re in rows:
        by_code.setdefault(code, []).append(
            (code, datestr, nav, name, plan, option, isin, isin_re))

    universe_codes = {r["amfi_code"] for r in load_universe() if r["amfi_code"]}

    # [NAV-STUB] batch-fill missing universe files with FULL AMFI histories
    # (R2 object first, then the AMFI portal walk under a strict per-run cap)
    # — never seed a thin recent-window file: it would shadow the full R2
    # history forever and analytics would honestly report "not enough NAV
    # history" for years. [DATA-POLICY: AMFI/AMC/NSE only.]
    missing = [c for c in by_code
               if c in universe_codes and not (out_dir / f"{c}.json").exists()]
    seeded_codes: set[str] = set()
    mirror_fetches = 0
    if missing:
        try:
            from webapp import remote_store
            for c in list(missing):
                if remote_store.ensure(f"nav_history/{c}.json") is not None:
                    seeded_codes.add(c)  # R2 served the full file
        except Exception:
            seeded_codes = set()
        still = [c for c in missing if c not in seeded_codes]
        batch = still[:MAX_FULL_HISTORY_FETCHES_PER_RUN]
        if batch:
            from .nav_history import fetch_codes_history
            summary = fetch_codes_history(batch, out_dir=out_dir,
                                          delay=PORTAL_WALK_DELAY)
            mirror_fetches = summary.get("written", 0)
            seeded_codes.update(batch)

    created = sum(1 for c in missing if (out_dir / f"{c}.json").exists())
    updated = unchanged = skipped = 0
    skipped_nonuniverse = skipped_unfilled = 0
    for code, pts in by_code.items():
        path = out_dir / f"{code}.json"
        if path.exists():
            doc, hist = _load_history(path)
            existing_dates = {h.get("date") for h in hist}
            new = [p for p in pts if p[1] not in existing_dates]
            if not new:
                unchanged += 1
                continue
            for _c, d, nav, *_ in new:
                hist.append({"date": d, "nav": nav})
            hist.sort(key=lambda h: _date_key(h.get("date", "")))
            doc["history"] = hist
            doc["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            path.write_text(json.dumps(doc), encoding="utf-8")
            updated += 1
        else:
            skipped += 1  # no file and none seeded — read path may fetch from R2
            # [NAV-FRESH] split for honest telemetry: the bulk of AMFI's 8.5k
            # codes are simply outside our tracked universe.
            if code not in universe_codes:
                skipped_nonuniverse += 1
            else:
                skipped_unfilled += 1

    from .nav_freshness import expected_latest_nav_date
    return {
        "window": f"{start.isoformat()}..{end.isoformat()}",
        # The newest NAV AMFI could have published when this run started —
        # on Monday mornings this is Friday, which is correct, not stale.
        "expected_latest": expected_latest_nav_date().isoformat(),
        "codes_seen": len(by_code),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "skipped_nonuniverse": skipped_nonuniverse,
        "skipped_unfilled": skipped_unfilled,
        "mirror_fetches": mirror_fetches,
        "total_nav_points": len(rows),
    }


def fill_gaps_from_last_known(out_dir: Path = OUT_DIR, max_age_days: int = 6,
                              max_window_days: int = 120,
                              deep_code_cap: int = 400) -> dict:
    """Per-scheme historical gap-fill: from each scheme's LAST KNOWN date to today.

    1. Scan nav_history files; a code is stale when its latest point is older
       than ``max_age_days``.
    2. Gaps within ``max_window_days`` are merged from ONE bulk AMFI fetch
       (oldest gap start -> today) for exactly those codes.
    3. Deeper gaps are delegated to the official chunked backfill
       (``nav_freshness.backfill_codes_amfi``), capped by ``deep_code_cap``
       so one pathological run can't hammer the portal.

    Returns a summary dict; telemetry under pipeline 'nav_gapfill'.
    """
    from .refresh_log import track
    with track("nav_gapfill", max_age_days=max_age_days) as meta:
        out_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()

        def _dkey(s):
            m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", str(s or "").strip())
            if not m:
                return None
            try:
                return datetime.strptime(
                    f"{int(m.group(1)):02d}-{m.group(2)[:3]}-{m.group(3)}",
                    "%d-%b-%Y").date()
            except ValueError:
                return None

        scanned_files = len(list(out_dir.glob("*.json")))
        stale_recent: dict[str, date] = {}
        stale_deep: dict[str, int] = {}
        for path in out_dir.glob("*.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                hist = doc.get("history") or []
            except Exception:
                continue
            last = hist[-1].get("date") if hist else None
            ld = _dkey(last)
            if ld is None:
                continue
            gap = (today - ld).days
            if gap > max_age_days:
                if gap <= max_window_days:
                    stale_recent[path.stem] = ld
                else:
                    stale_deep[path.stem] = gap
        meta.update(scanned=scanned_files,
                    stale_recent=len(stale_recent), stale_deep=len(stale_deep))

        merged_codes = 0
        points = 0
        if stale_recent:
            start = min(stale_recent.values()) - timedelta(days=3)
            text = _fetch_amfi(start, today)
            rows = _parse_nav_text(text)
            by_code: dict[str, list[tuple]] = {}
            for code, datestr, nav, *_ in rows:
                if code in stale_recent:
                    by_code.setdefault(code, []).append((datestr, nav))
            for code, pts in by_code.items():
                path = out_dir / f"{code}.json"
                doc, hist = _load_history(path)
                existing_dates = {h.get("date") for h in hist}
                new = [p for p in pts if p[1] not in existing_dates]
                if not new:
                    continue
                for dstr, nav in new:
                    hist.append({"date": dstr, "nav": nav})
                hist.sort(key=lambda h: _date_key(h.get("date", "")))
                doc["history"] = hist
                doc["fetched_at"] = datetime.now().isoformat(timespec="seconds")
                path.write_text(json.dumps(doc), encoding="utf-8")
                merged_codes += 1
                points += len(new)

        deep_summary = None
        if stale_deep:
            if len(stale_deep) > deep_code_cap:
                meta.update(deep_skipped=len(stale_deep))
            else:
                from .nav_freshness import backfill_codes_amfi
                deepest = max(stale_deep.values())
                try:
                    deep_summary = backfill_codes_amfi(
                        sorted(stale_deep), days=min(400, deepest + 10))
                except Exception as e:  # noqa: BLE001 - logged via track on raise?
                    meta.update(deep_error=str(e)[:200])

        result = {"scanned": meta.get("scanned", 0),
                  "stale_recent": len(stale_recent),
                  "stale_deep": len(stale_deep),
                  "merged_codes": merged_codes,
                  "points_added": points,
                  "deep_backfill": (deep_summary or {}).get("updated")
                  if isinstance(deep_summary, dict) else deep_summary}
        meta.update(result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10,
                        help="how many past days to fetch (default: 10)")
    args = parser.parse_args()
    summary = update_latest_navs(days=args.days)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
