"""Clean the Securities-directory source file ``data/reference/equity_isins.csv``.

The ``name`` and ``name_aliases`` columns were polluted with boilerplate/footnote
text scraped from AMFI / advisorkhoj holdings (e.g. ``^Note : BASE EXPENSE RATIO +
Statutory Levies. Adani Power Limited``, ``CONTRIBUTION BY MARKET CAP LIC Housing
Finance Ltd``) and with scheme/fund names, futures contracts and asset-class labels
leaked into ``name_aliases``.

This script rewrites the file so every security maps to its legitimate listed name:

* Any ISIN present in ``data/nifty/constituents/*.csv`` is remapped to the official
  NSE-listed company name + sector (and flagged ``confirmed_equity=1``).
* Remaining rows get their name scrubbed of footnote/boilerplate prefixes/suffixes.
* ``name_aliases`` keeps only genuine name variants of the company (dropping fund
  names, futures, footnote markers, asset-class labels).

Run from the repo root:

    python -m src.clean_equity_isins

The matching ``data/reference/equity_isin.db`` is rebuilt from the cleaned CSV so the
two reference files stay consistent (the webapp itself reads the CSV).
"""

from __future__ import annotations

import csv
import glob
import re
import sqlite3
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = BASE_DIR / "data" / "reference" / "equity_isins.csv"
DB_PATH = BASE_DIR / "data" / "reference" / "equity_isin.db"
CONSTITUENTS_DIR = BASE_DIR / "data" / "nifty" / "constituents"

# ---- footnote / boilerplate markers (lowercased) -----------------------------
_GARBAGE_MARKERS = re.compile(
    r"\^|\*|#|\$|~|\+"
    r"|fut[- ]"
    r"| years\)"
    r"|note\s*:"
    r"|statutory"
    r"|base expense"
    r"|contribution by"
    r"|portfolio details"
    r"|weighted harmonic"
    r"|experience in managing"
    r"|investment in foreign"
)
_FUND_MARKERS = re.compile(
    r"\b(?:mf|fund|etf|insurance scheme|direct plan|regular plan|growth plan|"
    r"dividend plan|index fund|exchange traded|arbitrage)\b"
    r"| - (?:direct|regular) plan\b"
)
_ASSET_LABELS = re.compile(r"\bequity\b|\bhybrid\b|\bdebt\b")
_INDEX_MARKERS = re.compile(
    r"\bindex\b|\bbenchmark\b|\bgilt\b|\bovernight\b|\b(?:1[0-9]|[0-9])\s*year\b"
)
_FRAGMENT_LEAD = re.compile(r"^\s*(?:years?|tri|overseas investments?|for large investors|limited|petrochemicals|products|hybrid composite)\b")
_MONTH_YEAR = re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{2,4}$")

# Prefixes that should be cut off before the real company name begins.
_NAME_PREFIXES = (
    "portfolio details",
    "note : base expense ratio + statutory levies.",
    "note :",
    "base expense ratio",
    "contribution by market cap",
    "equity & equity related",
    "eq -",
    "eq -",
)

# Suffixes / trailing annotation to strip from a name.
_NAME_SUFFIXES = (
    "(^)**",
    "(^)",
    "^^",
    " ^",
    "** #",
    "**",
    " #",
    " ^",
    "^",
    "*",
    "#",
)

# Tokens too generic to identify a company when matching aliases.
_STOP_TOKENS = {
    "ltd", "limited", "ltd.", "india", "indian", "group", "company", "companies",
    "co", "corp", "corporation", "and", "the", "of", "plc", "llp", "private",
    "pvt", "ltd.", "ltd", "energy", "enterprises", "enterprise", "services",
    "service", "systems", "solutions", "industries", "industry", "international",
    "ltd", "holding", "holdings",
}

# Legal trailing words for a company-name alias (entity forms + group markers).
_ENTITY_SUFFIX = {
    "ltd", "limited", "ltd.", "india", "indian", "group", "inc", "corp",
    "corporation", "co", "company", "plc", "llp", "pvt", "private",
    "holdings", "holding",
}


def _significant_tokens(name: str) -> set[str]:
    """Return the identifying tokens of a company name (case-insensitive)."""
    toks = set(re.findall(r"[a-z0-9]+", name.lower()))
    return {t for t in toks if len(t) >= 4 and t not in _STOP_TOKENS}


def _clean_name(raw: str, canon_name: str | None) -> str:
    """Return a scrubbed company name for a non-canonical row."""
    if not raw:
        return canon_name or ""
    name = raw
    lower = name.lower()
    # Cut known boilerplate prefixes.
    for prefix in _NAME_PREFIXES:
        if lower.startswith(prefix):
            rest = name[len(prefix):].lstrip(" .-:")
            if rest:
                name = rest
                lower = name.lower()
                break
    # Strip leading caret / punctuation.
    name = re.sub(r"^[\^#*$\s.\-:]+", "", name)
    # Strip trailing footnote markers & annotation.
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)].rstrip(" .-:").strip()
    # Drop leftover trailing parenthesised footnote like "(^)**" or "(^)".
    name = re.sub(r"\(\^+\)\*?$", "", name).rstrip(" .-:").strip()
    # Remove stray "(^)" markers anywhere and trailing footnote junk runs.
    name = re.sub(r"\(\^+\)", "", name)
    name = re.sub(r"[\s~#*$^]+$", "", name).rstrip(" .-:").strip()
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _is_fund_or_label(token: str) -> bool:
    low = token.lower()
    if _FUND_MARKERS.search(low) or _ASSET_LABELS.search(low):
        return True
    if low.startswith(("eq -", "equity", "hybrid", "debt")):
        return True
    return False


def _clean_aliases(raw_aliases: str, reference_name: str) -> list[str]:
    """Return a de-duplicated list of genuine name variants of ``reference_name``."""
    ref_tokens = _significant_tokens(reference_name)
    kept: list[str] = []
    seen: set[str] = set()
    for raw in (raw_aliases or "").split(";"):
        tok = raw.strip()
        if not tok:
            continue
        low = tok.lower()
        if _GARBAGE_MARKERS.search(low) or _FUND_MARKERS.search(low):
            continue
        if _INDEX_MARKERS.search(low) or _FRAGMENT_LEAD.match(low):
            continue
        if low.startswith(("eq -", "equity", "hybrid", "debt", "nifty")):
            continue
        if _is_fund_or_label(tok):
            continue
        # Must share at least one identifying token with the reference name,
        # otherwise it is a different entity (fund, parent group, etc.).
        if not (ref_tokens & _significant_tokens(tok)):
            continue
        # A genuine alias must lead with an identifying token of the company,
        # and (unless it is the plain company name) end on an entity/date suffix.
        words = [w for w in re.findall(r"[a-z0-9]+", low) if w]
        if not words or words[0] not in ref_tokens:
            continue
        last = words[-1]
        if len(words) > 2 and last not in _ENTITY_SUFFIX and last not in ref_tokens \
                and not _MONTH_YEAR.match(last):
            continue
        norm = re.sub(r"\s+", " ", low).strip(" .-:").lower()
        if norm in seen:
            continue
        seen.add(norm)
        kept.append(re.sub(r"\s+", " ", tok).strip(" .-:") or tok)
    return kept


def _canonical_map() -> dict[str, list[tuple[str, str]]]:
    """ISIN -> list of (company name, sector) from all Nifty constituent files."""
    out: dict[str, list[tuple[str, str]]] = {}
    for f in sorted(glob.glob(str(CONSTITUENTS_DIR / "*.csv"))):
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    isin = (r.get("ISIN Code") or r.get("ISIN") or "").strip()
                    name = (r.get("Company Name") or r.get("Company") or "").strip()
                    sector = (r.get("Industry") or "").strip()
                    if isin and name:
                        out.setdefault(isin, []).append((name, sector))
        except Exception:
            continue
    return out


def _best_canonical(cands: list[tuple[str, str]]) -> tuple[str, str]:
    """Pick the most representative (name, sector) for an ISIN."""
    name_counter = Counter(n for n, _ in cands)
    best_name = max(name_counter.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    sectors = [s for _, s in cands if s]
    sector = Counter(sectors).most_common(1)[0][0] if sectors else "na"
    return best_name, sector


# Official NSE market-cap index constituent files -> cap bucket (highest wins).
_CAP_BUCKETS = (
    ("NIFTY_100.csv", "large"),
    ("Nifty_Midcap_150.csv", "mid"),
    ("Nifty_Smallcap_250.csv", "small"),
    ("Nifty_Microcap_250.csv", "microcap"),
    ("Nifty_SME_EMERGE.csv", "sme"),
)


def _load_constituent_set(fname: str) -> set[str]:
    path = CONSTITUENTS_DIR / fname
    if not path.exists():
        return set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {row["ISIN Code"].strip() for row in csv.DictReader(fh) if (row.get("ISIN Code") or "").strip()}


def _nifty_cap_map() -> dict[str, str]:
    """ISIN -> official market-cap bucket, by NSE index membership (highest wins)."""
    isins: dict[str, str] = {}
    for fname, bucket in _CAP_BUCKETS:
        for isin in _load_constituent_set(fname):
            isins.setdefault(isin, bucket)
    return isins


def main() -> None:
    canon = _canonical_map()
    cap_map = _nifty_cap_map()

    with open(CSV_PATH, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    fieldnames = list(rows[0].keys()) if rows else [
        "isin", "name", "name_aliases", "source_count", "confirmed_equity", "cap", "sector",
    ]

    n_canon = n_renamed = 0
    n_cap_fixed = 0
    out_rows = []
    for r in rows:
        isin = (r.get("isin") or "").strip()
        raw_name = (r.get("name") or "").strip()
        aliases_raw = r.get("name_aliases") or ""
        sector = (r.get("sector") or "na").strip() or "na"
        confirmed_equity = (r.get("confirmed_equity") or "").strip()

        if isin in canon:
            canon_name, canon_sector = _best_canonical(canon[isin])
            name = canon_name
            if not sector or sector == "na":
                sector = canon_sector
            r["confirmed_equity"] = "1"
            n_canon += 1
            if raw_name != canon_name:
                n_renamed += 1
        else:
            name = _clean_name(raw_name, None)

        # For pure listed stocks, cap should come from the official NSE market-cap
        # index membership, not the upstream (often wrong) heuristic.
        if confirmed_equity == "1" and isin in cap_map:
            new_cap = cap_map[isin]
            if (r.get("cap") or "na").strip() != new_cap:
                n_cap_fixed += 1
            r["cap"] = new_cap

        aliases = _clean_aliases(aliases_raw, name or raw_name)
        # Always expose the canonical/cleaned name as an alias when it's not a
        # verbatim duplicate of the display name.
        if name and name.lower() not in {a.lower() for a in aliases}:
            aliases.insert(0, name)

        r["name"] = name
        r["name_aliases"] = "; ".join(aliases)
        r["sector"] = sector
        out_rows.append(r)

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    _rebuild_db(out_rows, fieldnames)

    print(f"Wrote {len(out_rows)} rows -> {CSV_PATH}")
    print(f"  canonical-remapped : {n_canon}  (renamed from garbage: {n_renamed})")
    print(f"  cap fixed (pure-listed) : {n_cap_fixed}")
    print(f"  aliases emptied    : {sum(1 for r in out_rows if not (r.get('name_aliases') or '').strip())}")


def _rebuild_db(rows: list[dict], fieldnames: list[str]) -> None:
    """Rebuild equity_isin.db from the cleaned CSV (keeps the two in sync)."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE equity_isins (
            isin TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_aliases TEXT,
            source_count INTEGER,
            confirmed_equity INTEGER,
            source_stores TEXT,
            cap TEXT DEFAULT 'na',
            sector TEXT DEFAULT 'na'
        )
        """
    )
    for r in rows:
        cur.execute(
            """
            INSERT OR REPLACE INTO equity_isins
                (isin, name, name_aliases, source_count, confirmed_equity,
                 source_stores, cap, sector)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                (r.get("isin") or "").strip(),
                (r.get("name") or "").strip(),
                (r.get("name_aliases") or "").strip(),
                int(r.get("source_count") or 0) if str(r.get("source_count") or "").strip().isdigit() else 0,
                int(r.get("confirmed_equity") or 0) if str(r.get("confirmed_equity") or "").strip().isdigit() else 0,
                (r.get("source_stores") or "").strip(),
                (r.get("cap") or "na").strip() or "na",
                (r.get("sector") or "na").strip() or "na",
            ),
        )
    con.commit()
    con.close()
    print(f"Rebuilt {DB_PATH}")


if __name__ == "__main__":
    main()
