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

# Known fund-house renames: the DB's amc column already carries the new
# house, but legacy fund_name strings keep the old brand (IDFC Mutual Fund
# -> Bandhan Mutual Fund, 2023). Applied to name tokens BEFORE matching so
# a renamed scheme ranks its true successor above same-named funds at other
# houses ('IDFC Infrastructure Fund' must resolve to BANDHAN Infrastructure
# Fund, never the HDFC lookalike). Extend as more houses rename.
HOUSE_RENAMES = {"idfc": "bandhan"}

# Generic words present in every AMC header ('... Mutual Fund') — stripped
# before house-token comparison so the same-house boost can't fire on them.
_AMC_GENERIC_TOKENS = {"mutual", "fund", "the"}


def _house_tokens(amc: str) -> set[str]:
    return set(norm_name(amc).split()) - _AMC_GENERIC_TOKENS


def _rename_house_tokens(tokens: list[str]) -> list[str]:
    return [HOUSE_RENAMES.get(t, t) for t in tokens]


def norm_name(name: str) -> str:
    """Lowercase, punctuation -> space, collapse. Keeps plan tokens
    (direct/regular/growth) so a scheme row matches ITS plan's code.
    Normalises the spelled-out ETF category ('exchange traded fund/scheme')
    to 'etf' so legacy names match their abbreviated renamed successors."""
    s = str(name or "").lower().replace("exchange traded fund", "etf") \
        .replace("exchange traded scheme", "etf")
    return _NONALNUM.sub(" ", s).strip()


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


def candidates(name: str, directory: dict[str, tuple[str, str]], top: int = 3,
               scheme_amc: str = "", scheme_plan: str = ""
               ) -> list[tuple[str, str, float, str]]:
    """Top-(top) (code, amfi_name, ratio, amc) candidates for one scheme name.

    Guards against wrong matches, in order of application:
    * house renames (HOUSE_RENAMES) are applied to the target tokens first;
    * candidates whose navall AMC header matches the scheme's own ``amc``
      get a +0.15 ratio boost — the DB already knows the correct house;
    * plan alignment: a Regular-plan scheme prefers 'regular plan' candidates
      and is penalised 'direct plan' ones (and vice versa);
    * ETF alignment: derived from the scheme's own name — a non-ETF scheme
      is heavily penalised against an ETF scheme (structural mismatch);
    * number tokens: shared series/limit numbers boost, no shared numbers
      penalise ('Series 129' must never resolve to 'Series 131').
    Matured FMPs whose codes are absent from today's directory surface as
    low-ratio best-effort rows — the duplicate warning and the ratio tell
    the reviewer not to approve them."""
    target_norm = norm_name(name)
    if not target_norm:
        return []
    t_tokens = _rename_house_tokens(target_norm.split())
    target = " ".join(t_tokens)
    t_token_set = set(t_tokens)
    # ETF alignment from the scheme's OWN name (the DB is_etf flag is
    # unreliable on legacy rows): 'IDFC Nifty Exchange Traded Fund'
    # normalises to an etf token; 'IDFC Nifty Fund' does not and must never
    # match an ETF scheme.
    wants_etf = "etf" in t_token_set
    amc_tokens = _house_tokens(scheme_amc) if scheme_amc else set()
    plan = (scheme_plan or "").strip().lower()
    want_plan = ("direct" if plan.startswith("direct")
                 else "regular" if plan.startswith("reg") else "")
    other_plan = ({"direct": "regular", "regular": "direct"}.get(want_plan)
                  or "")
    # Series/limit numbers are the most distinctive tokens a fund name has
    # ('Fixed Term Plan Series 129' vs '131'): sharing them is a strong signal,
    # not sharing them at all is a strong anti-signal.
    t_numbers = set(re.findall(r"\d+", target))
    exact = directory.get(target)
    scored: list[tuple[float, str, str, str]] = []
    if exact is not None:
        scored.append((1.0, exact[0], target, exact[1]))
    for norm, (code, amc) in directory.items():
        if norm == target:
            continue
        # cheap pre-filter: require some token overlap before the expensive ratio
        if not t_token_set & set(norm.split()):
            continue
        ratio = difflib.SequenceMatcher(None, target, norm).ratio()
        if amc_tokens and amc_tokens & _house_tokens(amc):
            ratio = min(1.0, ratio + 0.15)  # same fund house as the scheme row
        c_tokens = set(norm.split())
        if "etf" in c_tokens:
            if wants_etf:
                ratio = min(1.0, ratio + 0.05)
            else:
                # structural mismatch: a non-ETF scheme never maps to an ETF
                # scheme (different instrument/ISIN), however similar the name
                ratio = max(0.0, ratio - 0.3)
        if t_numbers:
            c_numbers = set(re.findall(r"\d+", norm))
            if t_numbers & c_numbers:
                ratio = min(1.0, ratio + 0.05)
            else:
                ratio = max(0.0, ratio - 0.2)  # e.g. Series 129 vs Series 131
        if want_plan:
            if want_plan in norm:
                ratio = min(1.0, ratio + 0.05)
            elif other_plan in norm:
                ratio = max(0.0, ratio - 0.05)
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
                                                 top=top,
                                                 scheme_amc=r["amc"] or "",
                                                 scheme_plan=r["plan"] or ""):
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
    # One AMFI code serves exactly one scheme — flag top-1 duplicates so the
    # reviewer doesn't approve two schemes onto the same fund by accident.
    top1: dict[str, int] = {}
    for r in out:
        if r["ratio"] >= 0.55:
            top1.setdefault(r["proposed_code"], 0)
            top1[r["proposed_code"]] += 1
    dupes = {c: n for c, n in top1.items() if n > 1}
    if dupes:
        print(f"  !! {len(dupes)} codes proposed for MULTIPLE schemes — "
              f"approve at most one row per code:")
        for c, n in sorted(dupes.items(), key=lambda kv: -kv[1])[:10]:
            names = [r["fund_name"] for r in out
                     if r["proposed_code"] == c and r["ratio"] >= 0.55]
            print(f"     {c}: {n} schemes -> {', '.join(names[:4])}")
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
        # [DATA-POLICY: AMFI/AMC/NSE only] full histories via the official
        # AMFI portal walk (one request covers every code per window).
        from src.nav_history import fetch_codes_history
        todo = [c for c in sorted(codes)
                if not (DATA_DIR / "nav_history" / f"{c}.json").exists()]
        if todo:
            summary = fetch_codes_history(todo, out_dir=DATA_DIR / "nav_history")
            for c in todo:
                out = DATA_DIR / "nav_history" / f"{c}.json"
                if out.exists():
                    n = len(json.loads(out.read_text(encoding="utf-8"))
                            .get("history") or [])
                    print(f"  fetched {n} pts -> {out.name}")
                else:
                    print(f"  no history fetched for {c}")
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
