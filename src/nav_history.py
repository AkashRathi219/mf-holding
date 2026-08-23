"""Build a database of historical daily NAVs for every mutual fund scheme.

Scope
-----
* Schemes: every fund+plan row in ``data/universe/Combined NAV - 14-Aug-2026.csv``
  (keyed by its AMFI scheme code, the ``Amficode`` column).
* History: full daily NAV back to the earliest AMFI publication (01-Apr-2006).
* Output: one JSON file per scheme under ``data/nav_history/<scheme_code>.json``.

Data sources (in order of preference)
-------------------------------------
1. **AMFI** (primary) - ``portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx``,
   official NAV history. A single call returns every scheme for a date range
   (max 90 days), so we backfill by walking 90-day windows from inception.
2. **Nifty** (fallback) - Nifty/NSE publish *index* data, not mutual-fund NAV;
   no MF NAV history is available there. Retained as a hook for completeness.
3. **Google Finance** (fallback) - no reliable public historical MF-NAV API.
   Retained as a hook; realistically AMFI covers all open/closed/interval schemes.

Because AMFI covers every scheme type, the fallbacks are only exercised for
scheme codes that end up with zero rows after the AMFI pass.

How it works
------------
* Rows are staged into a SQLite DB (``data/nav_history/.staging/nav.db``) so the
  backfill is resumable across runs (completed 90-day windows are remembered).
* After the AMFI pass, each scheme's rows are exported to a compact JSON file.
* A ``manifest.json`` + ``download_summary.json`` record what was fetched.

Run::

    python -m src.nav_history            # backfill (resumable) + export
    python -m src.nav_history --export   # export JSON only (skip download)
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sqlite3
import ssl
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UNIVERSE_CSV = BASE_DIR / "data" / "universe" / "Combined NAV - 14-Aug-2026.csv"
OUT_DIR = BASE_DIR / "data" / "nav_history"
STAGING_DIR = OUT_DIR / ".staging"
NAV_DB = STAGING_DIR / "nav.db"

AMFI_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
START_DATE = date(2006, 4, 1)      # earliest date AMFI serves NAV history
CHUNK_DAYS = 90                    # AMFI hard limit per request
REQUEST_DELAY = 1.0                # seconds between requests (be polite)
MAX_RETRIES = 5

_DATE_RE = re.compile(r"^\d{2}-[A-Z][a-z]{2}-\d{4}$")
_MONTH_KEYS = {m: f"{i:02d}" for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _date_key(datestr: str) -> str:
    """'31-Oct-2025' -> '2025-10-31' for chronological sorting."""
    try:
        day, mon, year = datestr.split("-")
        return f"{year}-{_MONTH_KEYS.get(mon.lower(), mon)}-{int(day):02d}"
    except Exception:
        return datestr


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _norm_code(raw: str) -> str:
    """Normalise an AMFI scheme code (may arrive as '154477.0').

    [DBT3] Only strips float artifacts ('154477.0' -> '154477'); a code that
    arrives as digits keeps its leading zeros ('012345' stays '012345') so it
    matches the universe/schemes tables exactly.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d+", raw):
        return raw
    try:
        f = float(raw)
    except ValueError:
        return raw
    return str(int(f)) if f.is_integer() else raw


def _resolve_universe_csv() -> Path:
    """Latest 'Combined NAV - *.csv' under data/universe/ (exact-name first,
    so a refreshed dated file keeps working without code changes)."""
    if UNIVERSE_CSV.exists():
        return UNIVERSE_CSV
    candidates = sorted(
        (p for p in UNIVERSE_CSV.parent.glob("Combined NAV - *.csv") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else UNIVERSE_CSV


def load_universe() -> list[dict]:
    """Return fund+plan rows (with a valid AMFI code) from the universe CSV."""
    rows = []
    csv_path = _resolve_universe_csv()
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            code = _norm_code(r.get("Amficode"))
            name = (r.get("Fund Name") or "").strip()
            if code.isdigit() and name:
                rows.append({
                    "amfi_code": code,
                    "fund_name": name,
                    "category": (r.get("Category") or "").strip(),
                    "plan": (r.get("Type") or "").strip(),
                    "as_of": (r.get("Data as of") or "").strip(),
                })
    return rows


def _init_db(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nav_history (
            scheme_code TEXT NOT NULL,
            date TEXT NOT NULL,
            nav REAL,
            name TEXT,
            plan TEXT,
            option TEXT,
            isin TEXT,
            isin_re TEXT,
            PRIMARY KEY (scheme_code, date)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_nav_scheme ON nav_history(scheme_code)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )


def _open_db() -> sqlite3.Connection:
    """Open the staging DB in WAL mode so multiple workers can write in parallel."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(NAV_DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks = []
    d = start
    while d <= end:
        c_end = min(d + timedelta(days=CHUNK_DAYS - 1), end)
        chunks.append((d, c_end))
        d = c_end + timedelta(days=1)
    return chunks


def _fetch_amfi(frm: date, tod: date) -> str:
    """Download one AMFI NAV-history window. Returns the raw text."""
    url = (f"{AMFI_URL}?tp=1&frmdt={frm.strftime('%d-%b-%Y')}"
           f"&todt={tod.strftime('%d-%b-%Y')}")
    ctx = _ssl_ctx()
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept-Encoding": "gzip",
                })
            with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            text = raw.decode("utf-8", "replace")
            if not text.lstrip().startswith("Scheme Code"):
                # AMFI returns an HTML error page when there is no data / error.
                if text.lstrip().lower().startswith(("<!doctype", "<html")):
                    return ""  # no data in this window
                raise RuntimeError("unexpected response (not NAV format)")
            return text
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            time.sleep(wait)
    raise RuntimeError(f"AMFI fetch failed {frm}..{tod}: {last_err}")


def _parse_nav_text(text: str) -> list[tuple]:
    """Parse AMFI NAV history text -> list of row tuples (8 fields)."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Scheme Code"):
            continue
        parts = line.split(";")
        if len(parts) != 8:
            continue
        code, name, plan, option, isin, isin_re, nav, datestr = parts
        code = code.strip()
        nav = nav.strip()
        datestr = datestr.strip()
        if not (code.isdigit() and _DATE_RE.match(datestr)):
            continue
        try:
            nav_f = float(nav)
        except ValueError:
            continue
        rows.append((code, datestr, nav_f, name.strip(), plan.strip(),
                     option.strip(), isin.strip(), isin_re.strip()))
    return rows


def backfill(target_codes: set[str], worker_id: int = 0, num_workers: int = 1) -> int:
    """Walk 90-day windows from inception to today, staging AMFI rows.

    With ``num_workers > 1`` this process only downloads the chunks assigned to
    ``worker_id`` (chunk index modulo ``num_workers``). All workers share the same
    WAL-mode SQLite staging DB, so they can run concurrently.
    """
    con = _open_db()
    cur = con.cursor()
    _init_db(cur)

    def _chunk_done(key: str) -> bool:
        return cur.execute(
            "SELECT 1 FROM meta WHERE key=?", (f"chunk:{key}",)).fetchone() is not None

    chunks = _chunks(START_DATE, date.today())
    mine = [c for i, c in enumerate(chunks) if i % num_workers == worker_id]
    todo = [c for c in mine if not _chunk_done(f"{c[0].isoformat()}|{c[1].isoformat()}")]
    print(f"Worker {worker_id}/{num_workers}: assigned {len(mine)} windows, "
          f"todo {len(todo)}", flush=True)

    total_inserted = 0
    for j, (frm, tod) in enumerate(todo, 1):
        key = f"{frm.isoformat()}|{tod.isoformat()}"
        text = _fetch_amfi(frm, tod)
        rows = _parse_nav_text(text)
        rows = [r for r in rows if r[0] in target_codes]
        if rows:
            for attempt in range(MAX_RETRIES):
                try:
                    # [DBT2] revision-aware upsert: when AMFI republishes a
                    # corrected NAV for (code, date), the new value REPLACES
                    # the old one instead of being silently ignored. The
                    # WHERE clause skips identical rows so routine re-fetches
                    # stay cheap.
                    cur.executemany(
                        """
                        INSERT INTO nav_history
                            (scheme_code,date,nav,name,plan,option,isin,isin_re)
                        VALUES (?,?,?,?,?,?,?,?)
                        ON CONFLICT(scheme_code,date) DO UPDATE SET
                            nav=excluded.nav, name=excluded.name,
                            plan=excluded.plan, option=excluded.option,
                            isin=excluded.isin, isin_re=excluded.isin_re
                        WHERE nav_history.nav IS NOT excluded.nav
                            OR nav_history.name IS NOT excluded.name
                        """, rows)
                    con.commit()
                    break
                except sqlite3.OperationalError:
                    time.sleep(2 ** attempt)
            total_inserted += len(rows)
        cur.execute("INSERT OR REPLACE INTO meta VALUES (?, '1')", (f"chunk:{key}",))
        con.commit()
        print(f"  W{worker_id} [{j}/{len(todo)}] {frm}..{tod}: {len(rows)} rows "
              f"(running {total_inserted})", flush=True)
        time.sleep(REQUEST_DELAY)

    con.close()
    return total_inserted


def _latest_row(cur, code: str):
    return cur.execute(
        "SELECT name, plan, option, isin, isin_re FROM nav_history "
        "WHERE scheme_code=? ORDER BY date DESC LIMIT 1", (code,)).fetchone()


def export_json() -> dict:
    """Write one JSON file per scheme; returns summary counts."""
    con = sqlite3.connect(NAV_DB)
    cur = con.cursor()
    codes = [r[0] for r in cur.execute(
        "SELECT DISTINCT scheme_code FROM nav_history ORDER BY scheme_code")]

    universe = {r["amfi_code"]: r for r in load_universe()}
    written = 0
    total_points = 0
    empty = []
    for code in codes:
        rows = cur.execute(
            "SELECT date, nav FROM nav_history WHERE scheme_code=? ORDER BY date",
            (code,)).fetchall()
        # 'DD-Mon-YYYY' sorts lexicographically (all day-01 rows first) — sort
        # chronologically so the exported series is a proper time series.
        rows = sorted(rows, key=lambda r: _date_key(r[0]))
        latest = _latest_row(cur, code)
        history = [{"date": d, "nav": n} for d, n in rows]
        u = universe.get(code, {})
        doc = {
            "scheme_code": code,
            "fund_name": u.get("fund_name") or (latest[0] if latest else ""),
            "category": u.get("category", ""),
            "plan": u.get("plan", "") or (latest[1] if latest else ""),
            "option": latest[2] if latest else "",
            "isin": latest[3] if latest else "",
            "isin_reinvestment": latest[4] if latest else "",
            "currency": "INR",
            "source": "AMFI",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "history": history,
        }
        (OUT_DIR / f"{code}.json").write_text(
            json.dumps(doc), encoding="utf-8")
        written += 1
        total_points += len(history)
        if not history:
            empty.append(code)
    con.close()

    # Schemes in the universe that got no AMFI rows (candidates for fallbacks).
    missing = sorted(set(universe) - set(codes))

    summary = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "schemes_with_data": written,
        "total_nav_points": total_points,
        "schemes_missing_from_amfi": len(missing),
        "missing_codes": missing[:500],
        "empty_schemes": empty,
    }
    (OUT_DIR / "download_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    manifest = {
        "title": "Historical Mutual Fund NAV database (AMFI)",
        "generated_at": summary["fetched_at"],
        "source": "AMFI India",
        "currency": "INR",
        "schemes_with_data": written,
        "total_nav_points": total_points,
        "files": [f"{c}.json" for c in codes],
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", action="store_true",
                        help="export JSON only (skip AMFI download)")
    parser.add_argument("--workers", type=int, default=1,
                        help="total number of parallel download workers")
    parser.add_argument("--worker-id", type=int, default=0,
                        help="this worker's index (0-based) when parallelising")
    args = parser.parse_args()

    universe = load_universe()
    target_codes = {r["amfi_code"] for r in universe}
    print(f"Universe scheme codes: {len(target_codes)}")

    if not args.export:
        inserted = backfill(target_codes,
                            worker_id=args.worker_id,
                            num_workers=max(1, args.workers))
        print(f"Worker {args.worker_id} done. {inserted} nav points staged.")

    # Only the coordinator (worker 0) exports, so shards aren't double-written.
    if args.worker_id == 0:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        summary = export_json()
        print(json.dumps(summary, indent=2))
        print(f"JSON written to {OUT_DIR}")


if __name__ == "__main__":
    main()
