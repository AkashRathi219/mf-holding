"""Accurate asset class breakup for all mutual fund scheme holdings.

Splits stocks from futures & options positions when hedging is involved
(F&O-v1 hedge sleeve), so each scheme shows a true Equity / Debt / Gold /
REITs-InvITs / Futures-&-Options / Cash / International / Other split.

Usage:
    python -m src.asset_class_breakup [--source amc_website|advisorkhoj|all]
    python -m src.asset_class_breakup --scheme HDFCMY
    python -m src.asset_class_breakup --json-out data/asset_breakup.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.db import (  # noqa: E402
    _INTL_ISIN_PREFIXES,
    classify_asset,
    refine_asset_class,
)
from src.excel_parser import _num  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
AMC_WEBSITES_DIR = DATA_DIR / "parsed" / "amc_websites"
ADVISORKHOJ_DIR = DATA_DIR / "parsed" / "advisorkhoj"
AMFI_DIR = DATA_DIR / "parsed" / "amfi"

_ASSET_LABELS = {
    "stocks": "Equity",
    "debt": "Debt",
    "gold": "Gold",
    "cash_equivalents": "Cash",
    "international": "International",
    "future_options": "Futures & Options",
    "other": "Other",
    "reits_invits": "REITs / InvITs",
    "fund_units": "Fund Units",
}

_REIT_INVIT_KEYWORDS = ("units issued by invits", "units issued by reit",
                        "reit", "invit", "real estate investment trust",
                        "infrastructure investment trust")

_GOLD_KEYWORDS = ("gold", "silver", "commodity")

# Sections that indicate debt even if not caught by classify_asset()
_DEBT_SECTION_KEYWORDS = ("debt", "bond", "money market", "certificate of deposit",
                          "commercial paper", "treasury", "g-sec", "gsec", "sdl",
                          "ncd", "securitised", "government security", "gilt",
                          "floating rate", "credit risk", "strips", "treps",
                          "bharat bond", "cp", "cd", "zero coupon",
                          "government securities", "non-convertible",
                          "securitized debt", "securitised debt")

# Sections that indicate cash equivalents
_CASH_SECTION_KEYWORDS = ("cash", "treps", "reverse repo", "short term deposit",
                          "net current assets", "net receivables", "cash & cash",
                          "bank deposit", "liquid fund", "money at call",
                          "money market instruments")

# Sections that indicate equity
_EQUITY_SECTION_KEYWORDS = ("equity", "stock", "shares", "preference", "warrant")


def _load_amc_website_schemes():
    if not AMC_WEBSITES_DIR.is_dir():
        return
    for p in sorted(AMC_WEBSITES_DIR.rglob("*.json")):
        if p.name.startswith("report_") or "factsheet" in p.name.lower():
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if isinstance(doc.get("metadata"), dict) and doc["metadata"].get("grouped_factsheet"):
            continue
        schemes = doc.get("schemes")
        if not isinstance(schemes, dict) or not schemes:
            continue
        amc = ((doc.get("amc_name") or "") or p.relative_to(AMC_WEBSITES_DIR).parts[0]).replace("_", " ").strip()
        for code, payload in schemes.items():
            if not isinstance(payload, dict):
                continue
            fund = payload.get("fund_name") or payload.get("scheme_name") or ""
            holdings = payload.get("holdings")
            if not isinstance(holdings, list) or not holdings:
                continue
            yield amc, "amc_website", payload.get("date"), fund, holdings


def _load_advisorkhoj_schemes():
    if not ADVISORKHOJ_DIR.is_dir():
        return
    for p in sorted(ADVISORKHOJ_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        amc = doc.get("amc") or p.stem
        for f in doc.get("files") or []:
            for sheet in f.get("sheets") or []:
                scheme_name = sheet.get("scheme")
                if not scheme_name:
                    continue
                merged = {}
                for plan in (sheet.get("plans") or {}).values():
                    for h in plan.get("holdings") or []:
                        merged.setdefault(h.get("isin") or h.get("name"), h)
                holdings = []
                for h in merged.values():
                    holdings.append({
                        "company": h.get("name") or "",
                        "isin": h.get("isin") or "",
                        "sector": h.get("industry") or h.get("rating") or "",
                        "quantity": h.get("quantity") or "",
                        "market_value": h.get("value") or "",
                        "percent_nav": h.get("pct_nav") or "",
                        "yield": h.get("yield") or "",
                        "section": h.get("section") or "",
                    })
                yield amc, "advisorkhoj", sheet.get("date"), scheme_name, holdings


def _load_amfi_schemes():
    if not AMFI_DIR.is_dir():
        return
    for p in sorted(AMFI_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        amc = (doc.get("amc") or p.stem).strip()
        as_of = doc.get("as_of") or ""
        schemes = doc.get("schemes")
        if not isinstance(schemes, dict):
            continue
        for fund, payload in schemes.items():
            if not isinstance(payload, dict) or not payload.get("holdings"):
                continue
            yield amc, "amfi", as_of, fund, payload["holdings"]


def _looks_like_holding(h: dict) -> bool:
    company = (h.get("company") or h.get("stock_name") or "").strip()
    isin = (h.get("isin") or "").strip()
    _HEADER_CELLS = {"date", "underlying", "scheme", "series", "issuer", "isin", "name",
                     "quantity", "qty", "market", "value", "market value", "pct", "pct_nav",
                     "percent", "rating", "industry", "sector", "symbol", "security",
                     "instrument", "description", "total", "sub total", "subtotal",
                     "grand total", "particulars", "details", "company", "asset class"}
    if company.lower() in _HEADER_CELLS or isin.lower() in _HEADER_CELLS:
        return False
    pct = _num(h.get("percent_nav") or h.get("pct_nav"))
    mv = _num(h.get("market_value") or h.get("value"))
    if isin and isin.lower() not in ("na", "-", "none"):
        return pct is None or abs(pct) <= 1000
    if pct is not None and abs(pct) <= 1000:
        return True
    if mv is not None:
        return True
    return False


_DEBT_NAME_INDICATORS = (
    "tier", "sdl", "goi", "g-sec", "gsec", "mat ", "mat%", "mat(",
    "msf", "cd ", "cp ", "repo", "treasury", "debenture", "ncd",
    "zero coupon", "commercial paper", "certificate of deposit",
    "bill ", "bond ", "note ", "fixed rate", "floating rate",
    "gilts", "gilts ", "sdf", "msf", "ndtb", "sss",
)


def _looks_like_debt_holding(company: str, section: str, isin: str) -> bool:
    """Heuristic: does a holding with empty section look like a debt instrument?"""
    name = company.lower()
    if any(k in name for k in _DEBT_NAME_INDICATORS):
        return True
    if "%" in name and any(c.isdigit() for c in name):
        return True
    if name.endswith("^") and not name.endswith("^ "):
        return True
    if isin.startswith("IN") and not isin.startswith("INE") and len(isin) > 10:
        return True
    return False


def _classify_holding(holding: dict) -> dict:
    """Classify a single holding into an asset class, returning an enriched dict.

    Follows the same logic as webapp.db.classify_asset() and
    webapp.db.refine_asset_class(), then adds REIT/InvITs/Gold detection
    and the F&O-v1 hedge sleeve split.
    """
    company = holding.get("company") or ""
    isin = holding.get("isin") or ""
    section = holding.get("section") or ""
    sector = holding.get("sector") or ""
    sec_lower = section.lower()
    name_lower = company.lower()

    # Step 1: Use classify_asset() from db.py (same logic as the webapp)
    asset_class = classify_asset(section, company, isin)

    # Step 2: Refine with security-directory tags
    if asset_class in ("", "other"):
        asset_class = refine_asset_class(asset_class, None, isin)

    # Step 3: If still unclassified or 'other', apply keyword-based fallback
    if asset_class in ("", "other"):
        # REITs / InvITs detection
        if any(k in sec_lower for k in _REIT_INVIT_KEYWORDS) or any(k in name_lower for k in _REIT_INVIT_KEYWORDS):
            asset_class = "reits_invits"
        # Gold detection
        elif any(k in sec_lower for k in _GOLD_KEYWORDS) or any(k in name_lower for k in _GOLD_KEYWORDS) or any(k in (sector or "").lower() for k in _GOLD_KEYWORDS):
            asset_class = "gold"
        # International (foreign ISIN prefixes)
        elif isin.startswith(_INTL_ISIN_PREFIXES):
            asset_class = "international"
        # Debt keywords
        elif any(k in sec_lower for k in _DEBT_SECTION_KEYWORDS):
            asset_class = "debt"
        # Cash equivalents keywords
        elif any(k in sec_lower for k in _CASH_SECTION_KEYWORDS):
            asset_class = "cash_equivalents"
        # Equity keywords
        elif any(k in sec_lower for k in _EQUITY_SECTION_KEYWORDS):
            asset_class = "stocks"
        # F&O: derivative expiry codes like 'AUG26'
        elif re.match(r"^[A-Z]{3}\d{2}$", isin.upper()):
            asset_class = "future_options"
        # Fund units (ISIN starting with INF)
        elif isin.startswith("INF"):
            asset_class = "fund_units"
        # ISIN-based fallback for empty-section holdings: check company name for debt indicators
        elif _looks_like_debt_holding(company, section, isin):
            asset_class = "debt"
        elif isin.startswith("INE"):
            asset_class = "stocks"
        elif isin.startswith("IN"):
            asset_class = "debt"

    # Step 4: [F&O-v1] Hedge sleeve split
    # percent_nav is GROSS exposure; derivative_pct_nav is the hedged slice.
    # Present net (unhedged) as effective stock weight and surface the hedge
    # as one aggregated futures_options entry.
    pct_nav = _num(holding.get("percent_nav"))
    hedged = _num(holding.get("derivative_pct_nav"))
    unhedged_raw = _num(holding.get("unhedged_pct_nav"))

    if asset_class == "stocks" and pct_nav is not None and (hedged or unhedged_raw is not None):
        has_split = bool(hedged) or unhedged_raw is not None
        if has_split:
            if unhedged_raw is None:
                unhedged = max(pct_nav - hedged, 0.0) if hedged else pct_nav
            else:
                unhedged = min(unhedged_raw, max(pct_nav - hedged, 0.0)) if hedged else unhedged_raw
            return {
                "company": company,
                "isin": isin,
                "section": section,
                "asset_class": "stocks",
                "percent_nav_gross": pct_nav,
                "percent_nav_effective": round(unhedged, 6),
                "pct_nav_hedged": round(hedged, 6) if hedged else None,
                "hedge_asset_class": "future_options",
                "hedge_pct_nav": round(hedged, 6) if hedged else None,
            }

    return {
        "company": company,
        "isin": isin,
        "section": section,
        "asset_class": asset_class,
        "percent_nav": pct_nav,
        "percent_nav_effective": pct_nav,
        "pct_nav_hedged": None,
        "hedge_asset_class": None,
        "hedge_pct_nav": None,
    }


def _build_scheme_breakup(holdings: list[dict]) -> dict:
    """Build asset class breakup for a single scheme from its holdings."""
    asset_totals: dict[str, float] = defaultdict(float)
    asset_holdings: dict[str, list[dict]] = defaultdict(list)
    hedge_total = 0.0
    total_nav = 0.0

    for h in holdings:
        if not _looks_like_holding(h):
            continue
        classified = _classify_holding(h)
        ac = classified["asset_class"]
        effective = classified["percent_nav_effective"]
        if effective is not None:
            asset_totals[ac] += effective
            total_nav += effective
        if classified.get("hedge_pct_nav") is not None:
            hedge_total += classified["hedge_pct_nav"]
            asset_totals["future_options"] += classified["hedge_pct_nav"]
            total_nav += classified["hedge_pct_nav"]
        asset_holdings[ac].append(classified)

    # Normalize to percentage if total_nav is close to 100
    if total_nav > 0 and abs(total_nav - 100.0) < 5:
        scale = 100.0 / total_nav
    else:
        scale = 1.0

    result = {}
    for ac in sorted(asset_totals.keys()):
        label = _ASSET_LABELS.get(ac, ac.title())
        raw_pct = asset_totals[ac] * scale
        result[ac] = {
            "label": label,
            "pct_nav": round(raw_pct, 4),
            "holdings_count": len(asset_holdings[ac]),
        }

    return {
        "asset_classes": result,
        "total_nav": round(total_nav * scale, 4),
        "hedge_total_pct_nav": round(hedge_total * scale, 4) if hedge_total else None,
    }


def run(source: str = "all", scheme_filter: str = None, json_out: str = None):
    """Run the asset class breakup analysis."""
    all_results = {}
    sources = []
    if source in ("all", "amc_website"):
        sources.append(("amc_website", _load_amc_website_schemes()))
    if source in ("all", "advisorkhoj"):
        sources.append(("advisorkhoj", _load_advisorkhoj_schemes()))
    if source in ("all", "amfi"):
        sources.append(("amfi", _load_amfi_schemes()))

    for src_name, gen in sources:
        for amc, src, as_of, fund, holdings in gen:
            if scheme_filter and scheme_filter.lower() not in fund.lower():
                continue
            key = f"{amc} - {fund}"
            if key in all_results:
                continue
            classified = [_classify_holding(h) for h in holdings]
            asset_totals: dict[str, float] = defaultdict(float)
            hedge_total = 0.0
            total_nav = 0.0
            for c in classified:
                ac = c["asset_class"]
                eff = c["percent_nav_effective"]
                if eff is not None:
                    asset_totals[ac] += eff
                    total_nav += eff
                if c.get("hedge_pct_nav") is not None:
                    hedge_total += c["hedge_pct_nav"]
                    asset_totals["future_options"] += c["hedge_pct_nav"]
                    total_nav += c["hedge_pct_nav"]

            if total_nav > 0 and abs(total_nav - 100.0) < 5:
                scale = 100.0 / total_nav
            else:
                scale = 1.0

            asset_breakup = {}
            for ac in sorted(asset_totals.keys()):
                label = _ASSET_LABELS.get(ac, ac.title())
                asset_breakup[ac] = {
                    "label": label,
                    "pct_nav": round(asset_totals[ac] * scale, 4),
                }

            all_results[key] = {
                "amc": amc,
                "fund_name": fund,
                "source": src_name,
                "as_of": as_of,
                "total_holdings": len([h for h in holdings if _looks_like_holding(h)]),
                "asset_breakup": asset_breakup,
                "total_nav": round(total_nav * scale, 4),
                "hedge_total_pct_nav": round(hedge_total * scale, 4) if hedge_total else None,
            }

    # Print summary
    print(f"\n{'='*80}")
    print(f"  ASSET CLASS BREAKUP - {len(all_results)} schemes")
    print(f"{'='*80}\n")

    for scheme_name, data in sorted(all_results.items()):
        print(f"--- {scheme_name} ({data['source']}, {data['as_of']}) ---")
        print(f"  Total holdings: {data['total_holdings']}")
        for ac, info in sorted(data["asset_breakup"].items(), key=lambda x: -x[1]["pct_nav"]):
            print(f"  {info['label']:<25s} {info['pct_nav']:>8.2f}%")
        if data["hedge_total_pct_nav"]:
            print(f"  {'(Hedge total)':<25s} {data['hedge_total_pct_nav']:>8.2f}%")
        print(f"  {'TOTAL':<25s} {data['total_nav']:>8.2f}%")
        print()

    if json_out:
        out_path = Path(json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        print(f"JSON output saved to: {out_path}")

    return all_results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Accurate asset class breakup for MF scheme holdings")
    parser.add_argument("--source", choices=["amc_website", "advisorkhoj", "amfi", "all"],
                        default="all", help="Data source to use")
    parser.add_argument("--scheme", default=None, help="Filter by scheme name")
    parser.add_argument("--json-out", default=None, help="Save JSON output to file")
    args = parser.parse_args()
    run(source=args.source, scheme_filter=args.scheme, json_out=args.json_out)