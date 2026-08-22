"""Reconcile the Combined-NAV "missing" funds against the ACTUAL parsed holdings.

The old pipeline (build_schemes_csv + find_missing_schemes) never merged the
advisorkhoj parsed schemes into its "known" set, so many funds may be wrongly
flagged missing.  This script re-checks every missing fund (from
missing_combined_nav.csv) directly against the scheme names present in BOTH
parsed stores:

  - data/parsed/advisorkhoj/*.json   (files[].sheets[].scheme)
  - data/parsed/amc_websites/**/*.json (schemes{} keys + fund_name)

and emits a reconciled missing list (only genuinely-uncovered funds remain).

Usage:
    python -m src.reconcile_missing
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = PROJECT_ROOT / "data" / "universe" / "Combined NAV - 14-Aug-2026.csv"
REGISTRY = PROJECT_ROOT / "config" / "amc_registry.json"
AK_PARSED = PROJECT_ROOT / "data" / "parsed" / "advisorkhoj"
WS_PARSED = PROJECT_ROOT / "data" / "parsed" / "amc_websites"
OUT = PROJECT_ROOT / "data" / "reference" / "reconciled_missing.csv"
OUT_ACTIVE = PROJECT_ROOT / "data" / "reference" / "reconciled_active_download.csv"
OUT_ND = PROJECT_ROOT / "data" / "reference" / "no_disclosure.csv"

# Funds the AMC does not publish a monthly portfolio for (verified) — kept out of
# the chaseable download backlog and reported separately.
NO_DISCLOSURE = {
    ("HSBC Mutual Fund", "HSBC Global Equity Climate Change FoF"),
    ("AlphaGrep Mutual Fund", "AlphaGrep Flexi Cap Fund"),
    ("AlphaGrep Mutual Fund", "AlphaGrep Liquid Omni FOF"),
    ("AlphaGrep Mutual Fund", "AlphaGrep Multi Asset Allocation Fund"),
    # NJ publishes monthly portfolios for only 5 schemes; Momentum/Value are not
    # NJ MF schemes (NJ site + full disclosure archive list only 5 funds).
    ("NJ Mutual Fund", "NJ Momentum Fund"),
    ("NJ Mutual Fund", "NJ Value Fund"),
    # Abakkus Large & Mid Cap is an NFO launched after the July-31 window; the
    # July monthly portfolio has no Large & Mid Cap sheet.
    ("Abakkus Mutual Fund", "Abakkus Large & Mid Cap Fund"),
    # Canara Robeco July-2026 monthly-portfolio list has only 10 scheme files;
    # Banking & Financial Services Fund is not among them (not published).
    ("Canara Robeco Mutual Fund", "Canara Robeco Banking and Financial Services Fund"),
}

MATCH_RATIO = 0.90
GENERIC = ("fund", "plan", "scheme", "portfolio", "growth", "direct", "regular")

# Scheme-name abbreviations the universe uses but parsed files spell out.
ABBR = [
    ("bbp", "baroda bnp paribas"),
    ("banking&fin serv", "banking and financial services"),
    ("banking & fin serv", "banking and financial services"),
    ("banking&financial serv", "banking and financial services"),
    ("fin serv", "financial services"),
    ("fina serv", "financial services"),
    ("infrastr", "infrastructure"),
    ("gei fund", "global equity income fund"),
    ("pee fund", "pan european equity fund"),
    ("gct fund", "global consumer trends fund"),
    ("dyf", "dividend yield"),
    ("div yieldfund", "dividend yield fund"),
    ("10y g-sec", "10 year g sec"),
    ("aap fund of fund", "asset allocation fund of fund"),
    ("overseas equity passive fof", "fund of funds"),
    ("retirement fund ig", "retirement fund income generation"),
    ("retirement fund wc", "retirement fund wealth creation"),
    ("retirement fund ap", "retirement fund aggressive plan"),
    ("retirement fund cp", "retirement fund conservative plan"),
    ("retirement fund dp", "retirement fund dynamic plan"),
    ("multi asset omni fof", "multi asset active fof"),
    ("sbi short horizon debt short term", "sbi short term debt fund"),
    ("icici pru", "icici prudential"),
    ("aditya birla sl", "aditya birla sun life"),
    ("ecoc reform", "economic reform"),
    ("oppt", "opportunity"),
    ("3to6", "3 to 6"),
    ("9to12", "9 to 12"),
    ("banking&financial services", "banking and financial services"),
    ("govt securities", "government securities"),
    ("retirement fund hybrid ap", "retirement fund hybrid aggressive plan"),
    ("retirement fund hybrid cp", "retirement fund hybrid conservative plan"),
    ("p h d", "pharma healthcare and diagnostics"),
    ("gilt invest", "gilt fund"),
    ("fof", "fund of funds"),
    ("liquid plan", "liquid fund"),
]


def expand_abbr(s: str) -> str:
    s = " " + s + " "
    for a, b in ABBR:
        s = s.replace(" " + a + " ", " " + b + " ")
    return re.sub(r"\s+", " ", s).strip()

# Name tokens stripped to reach the fund-level (Direct/Regular) identity.
PLAN_MARKERS = re.compile(
    r"\b(direct|regular|growth|idcw|dir|reg)\b|\((g|i[dw]cw|growth)\)|[-–]\s*(direct|regular)\b",
    re.IGNORECASE,
)

# Index / ETF / debt-benchmark detection tokens (for classification only).
IDX_TOKENS = ("index", "etf", "nifty", "bse", "sensex", "nasdaq", "msci", "s&p", "s and p")
DEBT_TOKENS = ("g-sec", "g sec", "gsec", "sdl", "ibx", "gilt", "1d rate", "aaa", "bond")
COMMODITY_TOKENS = ("gold", "silver")


def fund_level(name: str) -> str:
    s = PLAN_MARKERS.sub(" ", name)
    s = re.sub(r"\s+", " ", s).strip()
    return s or name


def classify(name: str) -> dict[str, bool]:
    n = norm(name)
    is_etf = bool(re.search(r"\betf\b", n))
    is_index = "index" in n or "nifty" in n or "sensex" in n or "bse " in n
    idx_based = is_etf or is_index or bool(re.search(r"\b(nasdaq|msci)\b", n))
    is_fof = "fof" in n or "fund of fund" in n or "fund of funds" in n
    return {
        "idx_based": "True" if idx_based else "False",
        "is_index": "True" if is_index else "False",
        "is_etf": "True" if is_etf else "False",
        "is_fof": "True" if is_fof else "False",
    }


def load_registry() -> list[dict]:
    import json as _json
    return _json.load(open(REGISTRY, encoding="utf-8-sig"))


def attribute_amc(fund: str, brands: list[tuple[str, str]]) -> str | None:
    """Return the registry AMC whose brand prefix matches the fund name."""
    n = norm(fund)
    for prefix, amc in brands:
        if n.startswith(prefix):
            return amc
    return None


def load_universe() -> list[dict]:
    """Read Combined NAV -> fund-level rows with AMC attribution + classification."""
    rows = list(csv.DictReader(UNIVERSE.open(encoding="utf-8-sig", newline="")))
    registry = load_registry()
    # Brand prefixes: longest-first so "360 ONE" beats shorter collisions.
    brands = sorted(
        ((norm(a["mf_name"].replace("Mutual Fund", "")), a["mf_name"]) for a in registry),
        key=lambda t: -len(t[0]),
    )
    seen: dict[str, dict] = {}
    for r in rows:
        name = r.get("Fund Name", "").strip()
        if not name:
            continue
        fund = fund_level(name)
        key = norm(fund)
        if key in seen:
            continue
        amc = attribute_amc(fund, brands)
        seen[key] = {
            "amc": amc or "",
            "fund": fund,
            "category": r.get("Category", ""),
            "asof": (r.get("Data as of") or "").split(" ")[0],
            **classify(fund),
        }
    return list(seen.values())


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def stripped(s: str) -> str:
    toks = norm(s).split()
    while toks and toks[-1] in GENERIC:
        toks.pop()
    return " ".join(toks)


def amc_key(amc: str) -> str:
    s = norm(amc)
    s = s.replace("mutual fund", "mf")
    return s


def load_advisorkhoj_schemes() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for jp in sorted(AK_PARSED.glob("*.json")):
        try:
            doc = json.load(open(jp, encoding="utf-8"))
        except Exception:
            continue
        amc = doc.get("amc")
        if not amc:
            continue
        key = amc_key(amc)
        for f in doc.get("files", []):
            for sheet in f.get("sheets", []):
                name = sheet.get("scheme") or ""
                if name:
                    out.setdefault(key, set()).add(norm(name))
    return out


def load_websites_schemes() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for jp in WS_PARSED.rglob("*.json"):
        try:
            doc = json.load(open(jp, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        amc = doc.get("amc_name")
        if not amc:
            continue
        key = amc_key(amc)
        schemes = doc.get("schemes") or {}
        for sname, sdata in schemes.items():
            for cand in (sdata.get("scheme_name"), sdata.get("fund_name"), sname):
                if cand:
                    out.setdefault(key, set()).add(norm(str(cand)))
    return out


def covered(fund: str, known: set[str]) -> bool:
    n = expand_abbr(norm(fund))
    if not n:
        return False
    variants = {n, stripped(fund)}
    for v in variants:
        if v in known:
            return True
    nt = set(n.split())
    for k in known:
        ke = expand_abbr(k)
        if len(ke.split()) < 2:
            continue
        if ke in n:
            return True
        if SequenceMatcher(None, n, ke).ratio() >= MATCH_RATIO:
            return True
        # Universe names are often the shortened form of the parsed full name
        # ("Bandhan Arbitrage" vs "Bandhan Arbitrage Fund", "DSP 10Y G-Sec" vs
        # "DSP 10Y G-Sec Fund").  If every universe token appears in a known
        # full name, treat as covered.
        if len(nt) >= 2 and nt <= set(ke.split()):
            return True
    return False


def main() -> None:
    ak = load_advisorkhoj_schemes()
    ws = load_websites_schemes()

    funds = load_universe()
    still_missing: list[dict] = []
    now_covered: list[dict] = []
    for f in funds:
        if not f["amc"]:
            continue
        key = amc_key(f["amc"])
        known = (ak.get(key, set()) | ws.get(key, set()))
        if covered(f["fund"], known):
            now_covered.append(f)
        else:
            still_missing.append(f)

    def wr(path, rows, fields):
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    fields = ["amc", "fund", "category", "idx_based", "is_index", "is_etf", "is_fof", "asof"]
    wr(OUT, [{k: r.get(k, "") for k in fields} for r in still_missing], fields)
    nd_keys = {(amc_key(a), norm(f)) for a, f in NO_DISCLOSURE}
    no_disclosure = [r for r in still_missing if (amc_key(r["amc"]), norm(r["fund"])) in nd_keys]
    active = [r for r in still_missing
              if (amc_key(r["amc"]), norm(r["fund"])) not in nd_keys
              and r.get("idx_based") != "True" and r.get("is_index") != "True" and r.get("is_etf") != "True"]
    wr(OUT_ACTIVE, [{k: r.get(k, "") for k in fields} for r in active], fields)
    wr(OUT_ND, [{k: r.get(k, "") for k in fields} for r in no_disclosure], fields)

    ak_names = sum(len(v) for v in ak.values())
    ws_names = sum(len(v) for v in ws.values())
    print(f"advisorkhoj parsed scheme names : {ak_names}")
    print(f"amc_websites parsed scheme names: {ws_names}")
    print(f"original missing                : {len(funds)}")
    print(f"  now COVERED (was false-miss)  : {len(now_covered)}")
    print(f"  still missing                 : {len(still_missing)}")
    active_still = len(active)
    print(f"  still missing ACTIVE (non-idx): {active_still}")
    print(f"  no-disclosure (excluded)      : {len(no_disclosure)}")
    print(f"\nwritten {OUT.name} ({len(still_missing)}), {OUT_ACTIVE.name} ({active_still}), {OUT_ND.name} ({len(no_disclosure)})")

    print("\nNow-covered funds (removed from backlog):")
    for r in sorted(now_covered, key=lambda x: (x["amc"], x["fund"])):
        print(f"  {r['amc'][:30]:30} {r['fund'][:60]}")


if __name__ == "__main__":
    main()
