"""Resolve missing AMFI codes for schemes by name-matching the AMFI directory.

88 schemes in webapp.db (old amc_website / advisorkhoj rows) carry no
``amfi_regular`` / ``amfi_direct`` code, so they have no NAV history and the
analytics engine honestly shows nothing. Many were renamed since (IDFC ->
Bandhan), so this is fuzzy matching with a HUMAN IN THE LOOP:

    python scripts/resolve_scheme_codes.py             # write review CSV
    python scripts/resolve_scheme_codes.py --apply     # apply approved rows
    python scripts/resolve_scheme_codes.py --apply --fetch   # + backfill NAVs

Review CSV (data/reference/scheme_code_resolution.csv): one row per
scheme x top candidate with a blank ``approved`` column. Fill ``approved``
with ``yes`` for the rows you accept, then run ``--apply``. Nothing is
written to the DB without that explicit yes.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "webapp.db"
NAVALL_TXT = DATA_DIR / "universe" / "navall.txt"
OUT_CSV = DATA_DIR / "reference" / "scheme_code_resolution.csv"

_NONALNUM = re.compile(r"[^a-z0-9]+")


def norm_name(name: str) -> str:
    """Lowercase, punctuation -> space, collapse. Keeps plan tokens
    (direct/regular/growth) so a scheme row matches ITS plan's code."""
    return _NONALNUM.sub(" ", str(name or "").lower()).strip()


def load_amfi_directory(path: Path = NAVALL_TXT) -> dict[str, tuple[str, str]]:
    """navall.txt -> {normalized scheme name: (code, amc_header)}.

    Data rows are ';'-separated with a numeric code first. AMFI groups rows
    under plain-text AMC headers ('... Mutual Fund') between category
    headers; tracking them lets the review CSV show WHICH fund house a
    candidate belongs to — renamed houses (IDFC -> Bandhan) and same-named
    funds at other houses are exactly the traps a human reviewer must see.
    """
    out: dict[str, tuple[str, str]] = {}
    current_amc = ""
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n").strip()
            if not line:
                continue
            parts = line.split(";")
            if len(parts) >= 4 and parts[0].strip().isdigit():
                name = parts[3].strip()
                if name:
                    out.setdefault(norm_name(name), (parts[0].strip(), current_amc))
                continue
            if line.lower().endswith("mutual fund"):
                current_amc = line
    return out


def candidates(name: str, directory: dict[str, tuple[str, str]], top: int = 3
               ) -> list[tuple[str, str, float, str]]:
    """Top-(top) (code, amfi_name, ratio, amc) candidates for one scheme name."""
    target = norm_name(name)
    if not target:
        return []
    exact = directory.get(target)
    scored: list[tuple[float, str, str, str]] = []
    if exact is not None:
        scored.append((1.0, exact[0], target, exact[1]))
    for norm, (code, amc) in directory.items():
        if norm == target:
            continue
        # cheap pre-filter: require some token overlap before the expensive ratio
        if not set(target.split()) & set(norm.split()):
            continue
        ratio = difflib.SequenceMatcher(None, target, norm).ratio()
        if ratio >= 0.55:
            scored.append((ratio, code, norm, amc))
    scored.sort(key=lambda t: -t[0])
    seen: set[str] = set()
    out: list[tuple[str, str, float, str]] = []
    for ratio, code, norm, amc in scored:
        if code in seen:
            continue
        seen.add(code)
        out.append((code, norm, round(ratio, 4), amc))
        if len(out) >= top:
            break
    return out


def build_review_csv(db_path: Path = DB_PATH,
                     directory: dict[str, tuple[str, str]] | None = None,
                     out_csv: Path = OUT_CSV, top: int = 3) -> int:
    if directory is None:
        directory = load_amfi_directory()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, fund_name, plan, source, amc FROM schemes "
        "WHERE (amfi_regular IS NULL OR amfi_regular='') "
        "AND (amfi_direct IS NULL OR amfi_direct='') ORDER BY id").fetchall()
    con.close()
    out = []
    for r in rows:
        for code, norm, ratio, amc in candidates(r["fund_name"], directory,
                                                 top=top):
            out.append({"scheme_id": r["id"], "fund_name": r["fund_name"],
                        "scheme_amc": r["amc"] or "", "plan": r["plan"] or "",
                        "source": r["source"], "proposed_code": code,
                        "proposed_name": norm, "proposed_amc": amc,
                        "ratio": ratio, "approved": ""})
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out[0].keys()) if out else
                                ["scheme_id", "fund_name", "scheme_amc", "plan",
                                 "source", "proposed_code", "proposed_name",
                                 "proposed_amc", "ratio", "approved"])
        writer.writeheader()
        writer.writerows(out)
    print(f"{len(rows)} code-less schemes -> {len(out)} candidate rows "
          f"in {out_csv}")
    print("review: set approved=yes on the rows you accept, then run --apply")
    return len(out)


def apply_approved(db_path: Path = DB_PATH, review_csv: Path = OUT_CSV,
                   fetch_history: bool = False) -> int:
    if not review_csv.exists():
        print(f"review CSV missing: {review_csv}")
        return 0
    with open(review_csv, encoding="utf-8-sig", newline="") as fh:
        approved = [r for r in csv.DictReader(fh)
                    if (r.get("approved") or "").strip().lower() == "yes"]
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    codes: set[str] = set()
    applied = 0
    for r in approved:
        sid, code = int(r["scheme_id"]), r["proposed_code"].strip()
        plan = (r.get("plan") or "").strip().lower()
        col = ("amfi_direct" if plan.startswith("direct")
               else "amfi_regular" if plan.startswith("reg")
               else None)
        if col:
            cur.execute(f"UPDATE schemes SET {col}=? WHERE id=?", (code, sid))
        else:  # plan unknown: ETFs/index rows share one code for both plans
            cur.execute("UPDATE schemes SET amfi_regular=?, amfi_direct=? "
                        "WHERE id=?", (code, code, sid))
        codes.add(code)
        applied += 1
    con.commit()
    con.close()
    print(f"applied {applied} approved rows ({len(codes)} distinct codes)")
    if fetch_history and codes:
        from src import fetch_missing_nav as fmn
        for code in sorted(codes):
            if (DATA_DIR / "nav_history" / f"{code}.json").exists():
                continue
            resp = fmn._fetch(code)
            if resp is None:
                print(f"  no history fetched for {code}")
                continue
            doc = fmn._build_doc(code, resp)
            out = DATA_DIR / "nav_history" / f"{code}.json"
            out.write_text(json.dumps(doc), encoding="utf-8")
            print(f"  fetched {len(doc.get('history') or [])} pts -> {out.name}")
    return applied


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="apply rows whose approved column is 'yes'")
    ap.add_argument("--fetch", action="store_true",
                    help="with --apply: also fetch full NAV history for new codes")
    ap.add_argument("--top", type=int, default=3,
                    help="candidates per scheme in the review CSV")
    args = ap.parse_args()
    if args.apply:
        apply_approved(fetch_history=args.fetch)
    else:
        build_review_csv(top=args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
