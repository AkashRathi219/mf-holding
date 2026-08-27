"""Independent arithmetic cross-check of build_annual_table (fund-table-v1.1.0).

Recomputes the table's per-year metrics and multi-year block straight from the
original annual records using the documented formulas in
docs/STOCK_ANALYTICS_METHODOLOGY.md §4.1 and compares against the engine's
own numbers. Catches formula drift / rounding regressions without trusting the
engine's own helpers.

Methodology it re-implements (from the docs):
  - margins = value / revenue (in %); ROE/ROA average prev+current when both
    exist; D/E uses the same averaged equity; FCF = CFO - capex;
  - YoY growth = (cur/prev - 1)*100;
  - CAGR over first -> last PRESENT value of the series (span = n-1);
  - cumulative FCF sums only the years where FCF is present;
  - net-debt change compares first -> last PRESENT (total_debt - cash) years,
    ignoring years where the balance-sheet items were not parsed.

Usage:
    python scripts/verify_fund_table.py           # report only
    python scripts/verify_fund_table.py --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp.stock_fundamental import build_annual_table  # noqa: E402

DOC_DIR = ROOT / "data" / "stock_financials"
REL = 0.06  # absolute tolerance on rounded percentages/ratios


def _safe_div(n, d):
    return None if (n is None or not d) else n / d


def _pct(n, d):
    """Percent with honest-None semantics (engine multiplies by 100 only when a value exists)."""
    v = _safe_div(n, d)
    return v * 100 if v is not None else None


def _r2(v):
    return round(v, 2) if v is not None else None


def _r1(v):
    return round(v, 1) if v is not None else None


def _avg_eq(ann, i):
    """Average equity rule from the docs: average prev+current when both exist."""
    eq = ann[i].get("total_equity")
    peq = ann[i - 1].get("total_equity") if i else None
    if eq is not None and peq is not None:
        return (eq + peq) / 2
    return eq


def recompute(doc: dict) -> dict:
    block = doc.get("consolidated") or doc.get("standalone") or {}
    ann = sorted([a for a in block.get("annual", []) if a.get("kind") == "FY"],
                 key=lambda a: a.get("period_end") or "")
    rev = [a.get("revenue_from_operations") for a in ann]
    pat = [a.get("pat") for a in ann]

    nm = [_r2(_pct(p, r)) for p, r in zip(pat, rev)]
    roe = [_r2(_pct(p, _avg_eq(ann, i))) for i, p in enumerate(pat)]
    de = [_r2(_safe_div(a.get("total_debt"), _avg_eq(ann, i)))
          for i, a in enumerate(ann)]
    cr = [_r2(_safe_div(a.get("total_current_assets"),
                        a.get("total_current_liabilities"))) for a in ann]
    fcf = [_r1(a["cfo"] - a["capex"])
           if None not in (a.get("cfo"), a.get("capex")) else None for a in ann]

    cagr = None
    if len(rev) >= 2 and rev[0] and rev[-1]:
        cagr = _r2((pow(rev[-1] / rev[0], 1 / (len(rev) - 1)) - 1) * 100)
    cumfcf = _r1(sum(fcf)) if len(fcf) >= 2 and all(v is not None for v in fcf) \
        else None
    nd = [(a.get("total_debt") - (a.get("cash_equivalents") or 0.0))
          if a.get("total_debt") is not None else None for a in ann]
    pres = [v for v in nd if v is not None]
    ndchg = _r1(pres[-1] - pres[0]) if len(pres) >= 2 else None

    return {"nm": nm, "roe": roe, "de": de, "cr": cr, "fcf": fcf,
            "cagr": cagr, "cumfcf": cumfcf, "ndchg": ndchg}


def mismatches(t: dict, o: dict) -> list[str]:
    bad = []
    n = t["years_n"]
    for y in range(n):
        m = t["columns"][y]["metrics"]
        for key, mk in (("net_margin_pct", "nm"), ("roe_pct", "roe"),
                        ("debt_to_equity", "de"), ("current_ratio", "cr"),
                        ("fcf_cr", "fcf")):
            v1, v2 = m[key], o[mk][y]
            if v1 is None and v2 is None:
                continue
            if v1 is None or v2 is None or abs(v1 - v2) > REL:
                bad.append(f"{key} {t['columns'][y]['fy']} engine={v1} "
                           f"recompute={v2}")
        if y and m["revenue_yoy_pct"] is not None:
            prev_rev = t["columns"][y - 1]["values"]["revenue_from_operations"]
            cur_rev = t["columns"][y]["values"]["revenue_from_operations"]
            want = _r2((cur_rev - prev_rev) / prev_rev * 100) \
                if prev_rev else None
            if want is not None and abs(m["revenue_yoy_pct"] - want) > REL:
                bad.append(f"yoy {t['columns'][y]['fy']}")
    g = (t["multi_year"]["cagr"].get("revenue") or {}).get("pct")
    if g is not None and o["cagr"] is not None and abs(g - o["cagr"]) > REL:
        bad.append(f"rev_cagr engine={g} recompute={o['cagr']}")
    cf = t["multi_year"]["cumulative_fcf_cr"]
    if cf is not None and o["cumfcf"] is not None and abs(cf - o["cumfcf"]) > 0.1:
        bad.append(f"cumfcf engine={cf} recompute={o['cumfcf']}")
    nd = t["multi_year"]["net_debt_change_cr"]
    if nd is not None and o["ndchg"] is not None and abs(nd - o["ndchg"]) > 0.1:
        bad.append(f"ndchg engine={nd} recompute={o['ndchg']}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checked = mismatched = 0
    error_docs = []
    detail = []
    for p in sorted(DOC_DIR.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            t = build_annual_table(doc)
            if not t.get("available") or t["years_n"] < 2:
                continue
            checked += 1
            bad = mismatches(t, recompute(doc))
            if bad:
                mismatched += 1
                detail.append((p.stem, bad[:8]))
        except Exception as exc:  # noqa: BLE001
            error_docs.append((p.stem, f"{type(exc).__name__}: {exc}"))

    out = {"checked": checked, "mismatched": mismatched,
           "error_docs": error_docs, "details": detail}
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"fund-table arithmetic cross-check: checked={checked} "
          f"mismatched={mismatched} engine_errors={len(error_docs)}")
    for stem, bad in detail[:20]:
        print(f"  {stem}: {'; '.join(bad)}")
    for stem, err in error_docs[:20]:
        print(f"  ERROR {stem}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())