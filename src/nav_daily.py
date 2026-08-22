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
  chronological order). New files are created for universe schemes that don't
  have one yet, so newly launched funds accumulate history from day one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .nav_history import _date_key, _fetch_amfi, _parse_nav_text, load_universe

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "nav_history"


def _load_history(path: Path) -> list[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc, doc.get("history") or []
    except Exception:
        return {}, []


def _latest_name_meta(pts: list[tuple]) -> dict:
    """Metadata (name/plan/option/isin) from the most recent parsed row, if any."""
    meta = {}
    for _code, _d, _nav, name, plan, option, isin, isin_re in pts:
        if name:
            meta = {"fund_name": name, "plan": plan, "option": option,
                    "isin": isin, "isin_reinvestment": isin_re}
    return meta


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

    created = updated = unchanged = skipped = 0
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
        elif code in universe_codes:
            hist = [{"date": p[1], "nav": p[2]} for p in pts]
            hist.sort(key=lambda h: _date_key(h.get("date", "")))
            meta = _latest_name_meta(pts)
            doc = {
                "scheme_code": code,
                "fund_name": meta.get("fund_name", ""),
                "category": "",
                "plan": meta.get("plan", ""),
                "option": meta.get("option", ""),
                "isin": meta.get("isin", ""),
                "isin_reinvestment": meta.get("isin_reinvestment", ""),
                "currency": "INR",
                "source": "AMFI",
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "history": hist,
            }
            path.write_text(json.dumps(doc), encoding="utf-8")
            created += 1
        else:
            skipped += 1  # non-universe scheme with no existing file — ignore

    return {
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "codes_seen": len(by_code),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "total_nav_points": len(rows),
    }


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
