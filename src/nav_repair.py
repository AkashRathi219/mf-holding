"""Repair NAV chunks that were lost to AMFI throttling.

The parallel backfill burst caused AMFI to return HTML error pages for some
90-day windows; those windows were wrongly marked "done" with zero rows. This
script finds every 90-day window that is marked done but has no rows staged in
that date range, clears the mark, and re-fetches it *sequentially* with retries
(HTML / network responses are treated as retryable failures, not "no data").

Run::

    python -m src.nav_repair
"""

from __future__ import annotations

import datetime
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.nav_history import (  # noqa: E402
    MAX_RETRIES,
    REQUEST_DELAY,
    START_DATE,
    _chunks,
    _fetch_amfi,
    _init_db,
    _open_db,
    _parse_nav_text,
    load_universe,
)


def _dates_in_db(con) -> set[str]:
    out = set()
    row = con.execute("SELECT DISTINCT date FROM nav_history")
    while True:
        batch = row.fetchmany(10000)
        if not batch:
            break
        for (d,) in batch:
            out.add(datetime.datetime.strptime(d, "%d-%b-%Y").date().isoformat())
    return out


def _chunk_has_data(dates: set[str], frm: date, tod: date) -> bool:
    d = frm
    while d <= tod:
        if d.isoformat() in dates:
            return True
        d += timedelta(days=1)
    return False


def _fetch_with_retry(frm: date, tod: date) -> str:
    """Fetch a window; retry on HTML/network responses (throttle), never treat
    an HTML error page as 'no data' for our date range."""
    last: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            text = _fetch_amfi(frm, tod)
            if text:  # valid NAV text
                return text
            # empty means AMFI returned an HTML page -> treat as throttle failure
            raise RuntimeError("AMFI returned HTML/error page (throttled)")
        except Exception as e:  # noqa: BLE001
            last = e
            wait = 3 ** attempt
            time.sleep(wait)
    raise RuntimeError(f"could not fetch {frm}..{tod} after {MAX_RETRIES} retries: {last}")


def repair(target_codes: set[str]) -> int:
    con = _open_db()
    cur = con.cursor()
    _init_db(cur)

    done = {
        r[0].replace("chunk:", "") for r in cur.execute("SELECT key FROM meta WHERE key LIKE 'chunk:%'")
    }
    chunks = _chunks(START_DATE, date.today())
    dates = _dates_in_db(con)

    empty = [c for c in chunks
             if f"{c[0].isoformat()}|{c[1].isoformat()}" in done
             and not _chunk_has_data(dates, *c)]

    print(f"Repairing {len(empty)} empty (throttled) windows...", flush=True)
    total = 0
    for i, (frm, tod) in enumerate(empty, 1):
        key = f"{frm.isoformat()}|{tod.isoformat()}"
        text = _fetch_with_retry(frm, tod)
        rows = [r for r in _parse_nav_text(text) if r[0] in target_codes]
        cur.executemany(
            # [DBT2] revision-aware upsert (mirrors nav_history worker).
            "INSERT INTO nav_history "
            "(scheme_code,date,nav,name,plan,option,isin,isin_re) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scheme_code,date) DO UPDATE SET nav=excluded.nav, "
            "name=excluded.name, plan=excluded.plan, option=excluded.option, "
            "isin=excluded.isin, isin_re=excluded.isin_re "
            "WHERE nav_history.nav IS NOT excluded.nav",
            rows)
        con.commit()
        total += len(rows)
        print(f"  [{i}/{len(empty)}] {frm}..{tod}: {len(rows)} rows "
              f"(running {total})", flush=True)
        time.sleep(REQUEST_DELAY)

    con.close()
    print(f"Repair complete. {total} additional nav points staged.")
    return total


def main() -> None:
    universe = load_universe()
    target_codes = {r["amfi_code"] for r in universe}
    print(f"Universe scheme codes: {len(target_codes)}")
    repair(target_codes)


if __name__ == "__main__":
    main()
