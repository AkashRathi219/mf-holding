"""Resolve index / ETF mutual funds to Nifty constituent holdings.

For each fund flagged idx_based / is_index / is_etf in the Combined-NAV universe,
try to map its benchmark to a Nifty constituent CSV (Company + ISIN) we already hold
in ``data/nifty/constituents/`` and emit those constituents as the fund's holdings.

Only *equity* Nifty indices have real ISIN constituents locally. Debt indices
(G-Sec / SDL / AAA / BHARAT Bond) expose index statistics only (no ISIN list), and
BSE / Nasdaq / MSCI / commodity benchmarks are not in our Nifty download, so those
are reported as unresolved with a reason.

Usage:
    python -m src.index_resolver
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .reconcile_missing import load_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSTITUENTS = PROJECT_ROOT / "data" / "nifty" / "constituents"
DEBT_CONSTITUENTS = PROJECT_ROOT / "data" / "nifty" / "debt_constituents"
OUT_HOLDINGS = PROJECT_ROOT / "data" / "reference" / "index_resolved_holdings.json"
OUT_UNRESOLVED = PROJECT_ROOT / "data" / "reference" / "index_unresolved.csv"

# Normalised keyword -> constituent CSV (equity indices with ISIN constituents).
# Order matters: most specific keywords first so "Nifty 200 Alpha 30" beats
# "Nifty 200" and "Nifty 500 Multicap 50:25:25" beats "Nifty 500".
INDEX_MAP = [
    ("nifty 500 multicap 50:25:25", "Nifty500_Multicap_50_25_25_Index.csv"),
    ("nifty smallcap250 momentum quality 100", "Nifty_Smallcap250_Momentum_Quality_100.csv"),
    ("nifty midsmallcap400 50:50", "Nifty_MidSmallcap400_50_50.csv"),
    ("nifty midsmallcap 400 50:50", "Nifty_MidSmallcap400_50_50.csv"),
    ("nifty 200 alpha 30", "Nifty200_Alpha_30.csv"),
    ("nifty 200 momentum 30", "Nifty200_Momentum_30.csv"),
    ("nifty largemidcap 250", "NIFTY_LargeMidcap_250.csv"),
    ("nifty capital markets", "Nifty_Capital_Markets.csv"),
    ("nifty oil and gas", "Nifty_Oil_and_Gas_Index.csv"),
    ("nifty midcap 150", "Nifty_Midcap_150.csv"),
    ("nifty smallcap 100", "NIFTY_Smallcap_100.csv"),
    ("nifty smallcap 250", "Nifty_Smallcap_250.csv"),
    ("nifty next 50", "NIFTY_Next_50.csv"),
    ("nifty 100", "NIFTY_100.csv"),
    ("nifty bank", "Nifty_Bank.csv"),
    ("nifty it", "Nifty_IT.csv"),
    ("nifty 50", "NIFTY_50.csv"),
]

# Debt / hybrid index constituents downloaded from NSE (+ AMC-published files) in
# data/nifty/debt_constituents/.  Keyword -> CSV.
DEBT_INDEX_MAP = [
    ("1d rate", "nifty_1d_rate.csv"),
    ("aaa psu bond plus sdl apr 2026 50:50", "nifty_aaa_psu_bond_plus_sdl_apr2026_5050.csv"),
    ("aaa bond plus sdl apr 2026 50:50", "nifty_aaa_bond_plus_sdl_apr2026_5050.csv"),
    ("sdl apr 2026 top 20 equal weight", "nifty_sdl_apr2026_top20_equalweight.csv"),
    ("largemidcap250 plus 8 13 yr g sec 70:30", "nifty_largemidcap250_plus_gsec_7030_gsec_leg.csv"),
    ("8 13 yr g sec", "nifty_8_13yr_gsec.csv"),
    ("short duration g sec", "nifty_short_duration_gsec.csv"),
]


def _norm(s: str) -> str:
    s = s.lower()
    s = s.replace("&", "and")
    # normalise spacing for common no-space index tokens
    s = re.sub(r"nifty\s*(\d{3})", r"nifty \1", s)          # nifty200 -> nifty 200
    s = re.sub(r"largemidcap\s*(\d{3})", r"largemidcap \1", s)   # largemidcap250
    s = re.sub(r"midsmallcap\s*(\d{3})", r"midsmallcap \1", s)
    s = re.sub(r"smallcap\s*(\d{3})", r"smallcap \1", s)
    s = re.sub(r"midcap\s*(\d{3})", r"midcap \1", s)
    s = re.sub(r"large\s*mid\s*cap", "largemidcap", s)      # large mid cap 250
    for tok in (
        "fund", "funds", "index fund", "etf", "exchange traded", "fof",
        "fund of fund", "fund of funds", "fo f", "fo fof", "plan", "growth",
        "direct", "regular", "-", "(", ")", " the ", " of ", " nse ",
    ):
        s = s.replace(tok, " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _load_constituents(csv_path: Path) -> list[dict]:
    """Return [{company, isin}] from an equity constituent file."""
    rows: list[dict] = []
    if not csv_path.exists():
        return rows
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            company = (r.get("Company Name") or "").strip()
            isin = (r.get("ISIN Code") or "").strip()
            if company and isin:
                rows.append({"company": company, "isin": isin})
    return rows


def _load_debt_constituents(csv_path: Path) -> list[dict]:
    """Return [{company, isin, weight}] from a debt/hybrid constituent file."""
    rows: list[dict] = []
    if not csv_path.exists():
        return rows
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            isin = (r.get("ISIN") or r.get("isin") or "").strip()
            if not isin:
                continue
            company = (r.get("SECURITY_NAME") or r.get("Issuer") or r.get("Security Name") or "").strip()
            weight = (r.get("Weight %") or r.get("Weight") or "").strip()
            rows.append({
                "company": company,
                "isin": isin,
                "weight": weight,
            })
    return rows


def resolve(name: str) -> tuple[str | None, str | None]:
    """Return (constituent_csv, matched_keyword) for an equity Nifty fund, else None."""
    n = _norm(name)
    for keyword, csv_name in INDEX_MAP:
        if keyword in n:
            return csv_name, keyword
    return None, None


def categorize(name: str) -> str | None:
    """Return a bucket for funds we can't resolve to a Nifty constituent list."""
    n = _norm(name)
    debt_tokens = ("g sec", "gsec", "sdl", "bond", "ibx", "gilt", "1d rate", "aaa")
    hybrid_tokens = ("plus 8 13", "plus 8 13yr", "plus 8 13 yr", "70 30", "75 25", "50 50")
    if any(t in n for t in hybrid_tokens) and ("g sec" in n or "gsec" in n):
        return "hybrid index (equity + g-sec) - partial constituents only"
    if any(t in n for t in debt_tokens):
        return "debt-index (no ISIN constituents locally; index stats only)"
    if "bse" in n or "sensex" in n:
        return "bse (not in Nifty download)"
    if "nasdaq" in n:
        return "nasdaq (US index; not in Nifty download)"
    if "msci" in n:
        return "msci (not in Nifty download)"
    if "gold" in n or "silver" in n:
        return "commodity (physical metal; no index constituents)"
    return "other"


def main() -> None:
    funds = load_universe()
    targets = [
        f for f in funds
        if f.get("idx_based") == "True" or f.get("is_index") == "True" or f.get("is_etf") == "True"
    ]

    holdings: dict[str, dict] = {}
    unresolved: list[dict] = []

    for f in sorted(targets, key=lambda r: (r["amc"], r["fund"])):
        amc, fund = f["amc"], f["fund"]
        csv_name, _kw = resolve(fund)
        is_debt = False
        if csv_name is None:
            for keyword, name in DEBT_INDEX_MAP:
                if keyword in _norm(fund):
                    csv_name, is_debt = name, True
                    break
        if csv_name:
            if is_debt:
                rows = _load_debt_constituents(DEBT_CONSTITUENTS / csv_name)
                source = "index-debt"
            else:
                rows = _load_constituents(CONSTITUENTS / csv_name)
                source = "index"
            holdings[f"{amc} | {fund}"] = {
                "amc": amc,
                "fund": fund,
                "index": csv_name.rsplit(".csv", 1)[0],
                "source": source,
                "as_of": f.get("asof", ""),
                "n_holdings": len(rows),
                "holdings": rows,
            }
        else:
            unresolved.append({
                "amc": amc,
                "fund": fund,
                "is_etf": f.get("is_etf"),
                "is_index": f.get("is_index"),
                "is_fof": f.get("is_fof"),
                "reason": categorize(fund),
            })

    OUT_HOLDINGS.write_text(
        json.dumps(holdings, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    with OUT_UNRESOLVED.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["amc", "fund", "is_etf", "is_index", "is_fof", "reason"])
        w.writeheader()
        w.writerows(unresolved)

    n_resolved = len(holdings)
    n_hold = sum(v["n_holdings"] for v in holdings.values())
    print(f"targets            : {len(targets)}")
    print(f"resolved (equity)  : {n_resolved}  -> {n_hold} ISIN holdings")
    print(f"unresolved locally : {len(unresolved)}")
    from collections import Counter
    for reason, cnt in Counter(u["reason"] for u in unresolved).most_common():
        print(f"   {cnt:3d}  {reason}")
    print(f"\nwrote {OUT_HOLDINGS.name}, {OUT_UNRESOLVED.name}")


if __name__ == "__main__":
    main()
