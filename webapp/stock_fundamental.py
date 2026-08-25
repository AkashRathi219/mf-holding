"""Fundamental-analysis engine [fund-v1.0.0].

Pure functions over a normalised statement document (data/stock_financials/
<ISIN>.json produced by src/financial_statements.py) plus the latest close
from the price history. House rules:

- Honest nulls: any ratio missing an input (or hitting a degenerate
  denominator <= 0) is None — never zero, never a fabricated fallback.
- TTM block drives "current" ratios; annual series drives growth and the
  two-period composite scores (Piotroski / Altman / Beneish).
- Values are Rs crore internally; per-share items are rupees. Shares
  outstanding are derived from the EPS bridge (PAT / basic EPS) and never
  invented when either side is missing.

Compliance: descriptive accounting arithmetic on filed public statements.
Not advice; callers render the standard disclaimer.
"""
from __future__ import annotations

import math

FUND_VERSION = "fund-v1.0.0"


def _f(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _ratio(num, den) -> float | None:
    n, d = _f(num), _f(den)
    if n is None or d is None or d == 0:
        return None
    return n / d


def _pct(num, den) -> float | None:
    r = _ratio(num, den)
    return None if r is None else round(r * 100.0, 2)


def _days(num, den) -> float | None:
    r = _ratio(num, den)
    return None if r is None else round(r * 365.0, 1)


def _cagr(end, start, years) -> float | None:
    e, s = _f(end), _f(start)
    if e is None or s is None or s <= 0 or e <= 0 or years <= 0:
        return None
    return round((e / s) ** (1.0 / years) - 1.0, 4)


# ---- document accessors ---------------------------------------------------------

def pick_block(doc: dict) -> dict | None:
    """Consolidated preferred, standalone fallback."""
    b = doc.get("consolidated") or doc.get("standalone")
    return b if isinstance(b, dict) else None


def _annual_series(block: dict) -> list[dict]:
    rows = [r for r in (block.get("annual") or []) if isinstance(r, dict)]
    return sorted(rows, key=lambda r: r.get("period_end") or "")


def _quarter_series(block: dict) -> list[dict]:
    rows = [r for r in (block.get("quarters") or [])
            if isinstance(r, dict) and not r.get("cumulative")]
    return sorted(rows, key=lambda r: r.get("period_end") or "")


def shares_outstanding(ttm: dict | None, annuals: list[dict],
                       quarters: list[dict] | None = None) -> float | None:
    """Absolute share count. Primary: EPS bridge (PAT / basic EPS).
    Fallback: paid-up share capital ÷ face value when the filing printed
    both (face value rides on the record as '_face_value')."""
    pat = _f((ttm or {}).get("pat"))
    eps = _f((ttm or {}).get("eps_basic"))
    if not pat or not eps or eps <= 0:
        if annuals:
            pat = _f(annuals[-1].get("pat"))
            eps = _f(annuals[-1].get("eps_basic"))
    if pat and eps and eps > 0:
        return pat * 1e7 / eps          # PAT Rs crore -> absolute shares
    for rec in sorted((quarters or []) + annuals,
                      key=lambda r: r.get("period_end") or "", reverse=True):
        sc = _f(rec.get("share_capital"))
        fv = _f(rec.get("_face_value"))
        if sc and fv and fv >= 0.5:
            return sc * 1e7 / fv
    return None


def derive_ebitda(rec: dict) -> float | None:
    ti = _f(rec.get("total_income")) or _f(rec.get("revenue_from_operations"))
    ex = _f(rec.get("total_expenses"))
    dep = _f(rec.get("depreciation_amortisation"))
    fin = _f(rec.get("finance_costs"))
    exc = _f(rec.get("exceptional_items")) or 0.0
    if None in (ti, ex, dep, fin):
        return None
    return ti - ex + dep + fin - exc


def _ebit(rec: dict) -> float | None:
    pbt, fin = _f(rec.get("pbt")), _f(rec.get("finance_costs"))
    return pbt + fin if None not in (pbt, fin) else None


def _avg_with_prev(ttm: dict, prev: dict | None, key: str):
    cur = _f(ttm.get(key))
    p = _f((prev or {}).get(key))
    if cur is not None and p is not None:
        return (cur + p) / 2.0
    return cur


# ---- per-share base -------------------------------------------------------------

def per_share_base(price: float | None, ttm: dict, latest_bs: dict,
                   shares: float | None) -> dict:
    out = {"eps": _f(ttm.get("eps_basic")),
           "dps": _f(ttm.get("dps")),
           "price": _f(price)}
    if shares:
        eq = _f(latest_bs.get("total_equity"))
        rev = _f(ttm.get("revenue_from_operations"))
        cfo = _f(ttm.get("cfo"))
        out["bvps"] = round(eq * 1e7 / shares, 2) if eq is not None else None
        out["sps"] = round(rev * 1e7 / shares, 2) if rev is not None else None
        out["cfps"] = round(cfo * 1e7 / shares, 2) if cfo is not None else None
        if price:
            mc = price * shares / 1e7
            out["market_cap_cr"] = round(mc, 1)
            debt = _f(latest_bs.get("total_debt"))
            cash = _f(latest_bs.get("cash_equivalents"))
            if debt is not None:
                nd = debt - (cash or 0.0)
                out["net_debt_cr"] = round(nd, 1)
                out["ev_cr"] = round(mc + nd, 1)
    return out


# ---- ratio families ---------------------------------------------------------------

def profitability(ttm: dict, prev_annual: dict | None) -> dict:
    rev = _f(ttm.get("revenue_from_operations"))
    ebitda = _f(ttm.get("ebitda")) or derive_ebitda(ttm)
    pat = _f(ttm.get("pat"))
    tax = _f(ttm.get("tax_expense"))
    pbt = _f(ttm.get("pbt"))
    equity = _avg_with_prev(ttm, prev_annual, "total_equity")
    assets = _avg_with_prev(ttm, prev_annual, "total_assets")
    debt = _f(ttm.get("total_debt"))
    ebit = _ebit(ttm)
    invested = (debt + equity) if None not in (debt, equity) else None
    tax_rate = _ratio(tax, pbt) if pbt else None
    roic = None
    if None not in (ebit, tax_rate, invested) and invested > 0:
        roic = _pct(ebit * (1 - min(max(tax_rate, 0.0), 1.0)), invested)
    cogs = _f(ttm.get("cost_of_materials"))
    gross = None
    if rev and cogs is not None:
        gross = round((rev - cogs) / abs(rev) * 100.0, 2)
    return {
        "gross_margin_pct": gross,
        "ebitda_margin_pct": _pct(ebitda, rev),
        "operating_margin_pct": _pct(ebit, rev),
        "net_margin_pct": _pct(pat, rev),
        "roe_pct": _pct(pat, equity),
        "roa_pct": _pct(pat, assets),
        "roce_pct": _pct(ebit, invested),
        "roic_pct": roic,
    }


def dupont(ttm: dict, prev_annual: dict | None) -> dict | None:
    rev = _f(ttm.get("revenue_from_operations"))
    pat = _f(ttm.get("pat"))
    pbt = _f(ttm.get("pbt"))
    assets = _avg_with_prev(ttm, prev_annual, "total_assets")
    equity = _avg_with_prev(ttm, prev_annual, "total_equity")
    npm = _ratio(pat, rev)
    ato = _ratio(rev, assets)
    em = _ratio(assets, equity)
    if None in (npm, ato, em):
        return None
    out = {
        "npm": round(npm, 4),
        "asset_turnover": round(ato, 3),
        "equity_multiplier": round(em, 2),
        "roe_check": round(npm * ato * em, 4),
    }
    tax_burden = _ratio(pat, pbt)
    ebit = _ebit(ttm)
    interest_burden = _ratio(pbt, ebit)
    if None not in (tax_burden, interest_burden):
        out["tax_burden"] = round(tax_burden, 4)
        out["interest_burden"] = round(interest_burden, 4)
        out["roe_5way"] = round(
            tax_burden * interest_burden * npm * ato * em, 4)
    return out


def liquidity(ttm: dict) -> dict:
    ca = _f(ttm.get("total_current_assets"))
    cl = _f(ttm.get("total_current_liabilities"))
    inv = _f(ttm.get("inventory"))
    cash = _f(ttm.get("cash_equivalents"))
    rev = _f(ttm.get("revenue_from_operations"))
    wc = ca - cl if None not in (ca, cl) else None
    return {
        "current_ratio": round(ca / cl, 2) if ca is not None and cl else None,
        "quick_ratio": (round((ca - inv) / cl, 2)
                        if None not in (ca, inv, cl) and cl else None),
        "cash_ratio": (round(cash / cl, 2)
                       if None not in (cash, cl) and cl else None),
        "wc_to_sales_pct": _pct(wc, rev),
    }


def leverage(ttm: dict) -> dict:
    debt = _f(ttm.get("total_debt"))
    eq = _f(ttm.get("total_equity"))
    cash = _f(ttm.get("cash_equivalents"))
    ebitda = _f(ttm.get("ebitda")) or derive_ebitda(ttm)
    fin = _f(ttm.get("finance_costs"))
    ebit = _ebit(ttm)
    nd = debt - cash if None not in (debt, cash) else \
        (debt if debt is not None else None)
    return {
        "debt_to_equity": (round(debt / eq, 2)
                           if debt is not None and eq else None),
        "debt_to_ebitda": (round(debt / ebitda, 2)
                           if debt is not None and ebitda else None),
        "net_debt_to_ebitda": (round(nd / ebitda, 2)
                               if nd is not None and ebitda else None),
        "interest_coverage": (round(ebit / fin, 1)
                              if ebit is not None and fin and fin > 0
                              else None),
    }


def efficiency(ttm: dict) -> dict:
    rev = _f(ttm.get("revenue_from_operations"))
    cogs = _f(ttm.get("cost_of_materials"))
    inv = _f(ttm.get("inventory"))
    recv = _f(ttm.get("trade_receivables"))
    pay = _f(ttm.get("trade_payables"))
    assets = _f(ttm.get("total_assets"))
    inv_d = _days(inv, cogs)
    recv_d = _days(recv, rev)
    pay_d = _days(pay, cogs)
    ccc = (round(inv_d + recv_d - pay_d, 1)
           if None not in (inv_d, recv_d, pay_d) else None)
    return {
        "asset_turnover": _ratio(rev, assets),
        "inventory_turnover": _ratio(cogs, inv),
        "inventory_days": inv_d,
        "receivable_days": recv_d,
        "payable_days": pay_d,
        "cash_conversion_cycle": ccc,
    }


def cashflow_quality(ttm: dict) -> dict:
    cfo = _f(ttm.get("cfo"))
    capex = _f(ttm.get("capex"))
    pat = _f(ttm.get("pat"))
    rev = _f(ttm.get("revenue_from_operations"))
    ebitda = _f(ttm.get("ebitda")) or derive_ebitda(ttm)
    fcf = cfo - capex if None not in (cfo, capex) else None
    return {
        "fcf_cr": round(fcf, 1) if fcf is not None else None,
        "fcf_margin_pct": _pct(fcf, rev),
        "ocf_to_pat": (round(cfo / pat, 2)
                       if cfo is not None and pat else None),
        "capex_to_sales_pct": _pct(capex, rev),
        "fcf_to_ebitda_pct": _pct(fcf, ebitda),
    }


def growth(annuals: list[dict]) -> dict:
    out = {"revenue_yoy": None, "pat_yoy": None, "eps_yoy": None,
           "revenue_cagr_3y": None, "pat_cagr_3y": None,
           "positive_revenue_years": None}
    if len(annuals) < 2:
        return out
    cur, prev = annuals[-1], annuals[-2]
    for key, name in (("revenue_from_operations", "revenue_yoy"),
                      ("pat", "pat_yoy"), ("eps_basic", "eps_yoy")):
        c, p = _f(cur.get(key)), _f(prev.get(key))
        if None not in (c, p) and p != 0:
            out[name] = round(c / abs(p) - 1.0, 4)
    span = min(3, len(annuals) - 1)
    base = annuals[-1 - span]
    out["revenue_cagr_3y"] = _cagr(annuals[-1].get("revenue_from_operations"),
                                   base.get("revenue_from_operations"), span)
    out["pat_cagr_3y"] = _cagr(annuals[-1].get("pat"),
                               base.get("pat"), span)
    revs = [_f(a.get("revenue_from_operations")) for a in annuals]
    pairs = [(a, b) for a, b in zip(revs, revs[1:])
             if None not in (a, b) and a > 0]
    if pairs:
        out["positive_revenue_years"] = round(
            sum(1 for a, b in pairs if b > a) / len(pairs), 2)
    return out


def valuation(ps: dict) -> dict:
    price = ps.get("price")
    ev = ps.get("ev_cr")
    ebitda = ps.get("_ebitda")
    out = {
        "pe": (_ratio(price, ps.get("eps"))
               if price and ps.get("eps") and ps["eps"] > 0 else None),
        "pb": (_ratio(price, ps.get("bvps"))
               if price and ps.get("bvps") and ps["bvps"] > 0 else None),
        "ps": (_ratio(price, ps.get("sps"))
               if price and ps.get("sps") and ps["sps"] > 0 else None),
        "pcf": (_ratio(price, ps.get("cfps"))
                if price and ps.get("cfps") and ps["cfps"] > 0 else None),
        "dividend_yield_pct": (_pct(ps.get("dps"), price)
                               if price and ps.get("dps") else None),
        "earnings_yield_pct": (_pct(ps.get("eps"), price)
                               if price and ps.get("eps")
                               and ps["eps"] > 0 else None),
        "ev_ebitda": (_ratio(ev, ebitda)
                      if ev is not None and ebitda and ebitda > 0 else None),
        "peg": None,
        "graham_number": None,
    }
    pe, g = out["pe"], ps.get("_rev_cagr")
    if pe and g is not None and g > 0:
        out["peg"] = round(pe / (g * 100.0), 2)
    eps, bvps = ps.get("eps"), ps.get("bvps")
    if eps and eps > 0 and bvps and bvps > 0:
        out["graham_number"] = round(math.sqrt(22.5 * bvps * eps), 2)
    return out


# ---- composite scores -------------------------------------------------------------

def piotroski_f(cur: dict, prev: dict) -> dict | None:
    """9-point F-Score; each test is True/False/None (n/a when inputs miss).
    Score sums only the evaluable tests."""
    if not prev:
        return None

    def f(rec, k):
        return _f(rec.get(k))

    roa_c = _ratio(f(cur, "pat"), f(cur, "total_assets"))
    roa_p = _ratio(f(prev, "pat"), f(prev, "total_assets"))
    cfo_c, pat_c = f(cur, "cfo"), f(cur, "pat")
    lev_c = _ratio(f(cur, "total_debt"), f(cur, "total_equity"))
    lev_p = _ratio(f(prev, "total_debt"), f(prev, "total_equity"))

    def gm(rec):
        rev = f(rec, "revenue_from_operations")
        cogs = f(rec, "cost_of_materials")
        return _ratio(rev - cogs, rev) if rev else None

    ato_c = _ratio(f(cur, "revenue_from_operations"), f(cur, "total_assets"))
    ato_p = _ratio(f(prev, "revenue_from_operations"),
                   f(prev, "total_assets"))

    def b(name, val):
        return name, (None if val is None else int(bool(val)))

    tests = dict([
        b("roa_positive", roa_c is not None and roa_c > 0),
        b("cfo_positive", cfo_c is not None and cfo_c > 0),
        b("roa_improving", None if None in (roa_c, roa_p)
          else roa_c > roa_p),
        b("accruals_quality", None if None in (cfo_c, pat_c)
          else cfo_c > pat_c),
        b("leverage_down", None if None in (lev_c, lev_p) or lev_p in (None,)
          or lev_c is None or lev_p is None
          else lev_c < lev_p),
        b("current_ratio_up",
          None if None in (f(cur, "total_current_assets"),
                           f(cur, "total_current_liabilities"),
                           f(prev, "total_current_assets"),
                           f(prev, "total_current_liabilities"))
          or not f(prev, "total_current_liabilities")
          or not f(cur, "total_current_liabilities")
          else (_ratio(f(cur, "total_current_assets"),
                       f(cur, "total_current_liabilities")) >
                _ratio(f(prev, "total_current_assets"),
                       f(prev, "total_current_liabilities")))),
        b("no_dilution",
          None if None in (f(cur, "eps_basic"), f(prev, "eps_basic"))
          or not f(prev, "eps_basic")
          else f(cur, "shares_proxy") is None and
          f(cur, "eps_basic") >= f(prev, "eps_basic")),
        b("gross_margin_up",
          None if None in (gm(cur), gm(prev)) else gm(cur) > gm(prev)),
        b("turnover_up",
          None if None in (ato_c, ato_p) else ato_c > ato_p),
    ])
    score = sum(v for v in tests.values() if v is not None)
    evaluable = sum(1 for v in tests.values() if v is not None)
    return {"score": score, "out_of": 9, "evaluable": evaluable,
            "tests": tests}


def altman_z(ttm: dict, market_cap_cr: float | None) -> dict | None:
    """Public-firm Z: 1.2 WC/TA + 1.4 RE/TA + 3.3 EBIT/TA + 0.6 MktCap/TL
    + 1.0 Sales/TA. Honest null unless every term computes."""
    ta = _f(ttm.get("total_assets"))
    if not ta or ta <= 0:
        return None
    ca, cl = _f(ttm.get("total_current_assets")), \
        _f(ttm.get("total_current_liabilities"))
    re_ = _f(ttm.get("reserves_surplus"))
    ebit = _ebit(ttm)
    eq = _f(ttm.get("total_equity"))
    tl = _f(ttm.get("total_liabilities"))
    if tl is None and eq is not None:
        tl = ta - eq                       # A = E + L identity fallback
    sales = _f(ttm.get("revenue_from_operations"))
    wc = ca - cl if None not in (ca, cl) else None
    terms = {
        "wc_ta": _ratio(wc, ta), "re_ta": _ratio(re_, ta),
        "ebit_ta": _ratio(ebit, ta),
        "mcap_tl": (_ratio(market_cap_cr, tl)
                    if market_cap_cr is not None and tl else None),
        "sales_ta": _ratio(sales, ta),
    }
    weights = {"wc_ta": 1.2, "re_ta": 1.4, "ebit_ta": 3.3,
               "mcap_tl": 0.6, "sales_ta": 1.0}
    z = 0.0
    for k, w in weights.items():
        v = terms[k]
        if v is None:
            return None
        z += w * v
    zone = ("distress" if z < 1.81 else "grey" if z < 2.99 else "safe")
    return {"z": round(z, 3), "zone": zone, "terms": terms}


def beneish_m(cur: dict, prev: dict) -> dict | None:
    """8-variable M-Score; needs a comparable prior year. M > -1.78 flags
    elevated manipulation risk (statistical prior, NOT an accusation)."""
    if not prev:
        return None

    def f(rec, k):
        return _f(rec.get(k))

    def gmargin(rec):
        rev, cogs = f(rec, "revenue_from_operations"), f(rec, "cost_of_materials")
        return _ratio(rev - cogs, rev) if rev else None

    dsri = None
    if None not in (f(cur, "trade_receivables"), f(cur, "revenue_from_operations"),
                    f(prev, "trade_receivables"), f(prev, "revenue_from_operations")) \
            and f(prev, "trade_receivables") and f(prev, "revenue_from_operations"):
        cur_r = _ratio(f(cur, "trade_receivables"),
                       f(cur, "revenue_from_operations"))
        prev_r = _ratio(f(prev, "trade_receivables"),
                        f(prev, "revenue_from_operations"))
        dsri = _ratio(cur_r, prev_r)
    gmi = None
    if None not in (gmargin(prev), gmargin(cur)) and gmargin(prev):
        gmi = _ratio(gmargin(prev), gmargin(cur))
    sgi = None
    if f(prev, "revenue_from_operations"):
        sgi = _ratio(f(cur, "revenue_from_operations"),
                     f(prev, "revenue_from_operations"))
    depi = None
    if None not in (f(cur, "depreciation_amortisation"),
                    f(prev, "depreciation_amortisation")) \
            and f(prev, "depreciation_amortisation"):
        depi = _ratio(_ratio(f(prev, "depreciation_amortisation"),
                             f(prev, "ppe_net") or 1),
                      _ratio(f(cur, "depreciation_amortisation"),
                             f(cur, "ppe_net") or 1))
    sgai = None
    if None not in (f(cur, "other_expenses"), f(prev, "other_expenses")) \
            and f(prev, "other_expenses"):
        sgai = _ratio(
            _ratio(f(cur, "other_expenses"), f(cur, "revenue_from_operations") or 1),
            _ratio(f(prev, "other_expenses"), f(prev, "revenue_from_operations") or 1))
    lvgi = None
    if f(prev, "total_liabilities") and f(prev, "total_assets"):
        lvgi = _ratio(_ratio(f(cur, "total_liabilities"), f(cur, "total_assets") or 1),
                      _ratio(f(prev, "total_liabilities"), f(prev, "total_assets") or 1))
    tata = None
    pbt, cfo = f(cur, "pbt"), f(cur, "cfo")
    ta = f(cur, "total_assets")
    if None not in (pbt, cfo, ta) and ta:
        tata = (pbt - cfo) / abs(ta)

    parts = {"dsri": dsri, "gmi": gmi, "sgi": sgi, "depi": depi,
             "sgai": sgai, "lvgi": lvgi, "tata": tata}
    required = ("dsri", "gmi", "sgi", "tata")
    if any(parts[k] is None for k in required):
        return None
    m = (-4.84 + 0.920 * (parts["dsri"] or 0) + 0.528 * (parts["gmi"] or 0)
         + 0.404 * ((parts.get("aqi") or 1.0)) + 0.892 * (parts["sgi"] or 1)
         + 0.115 * (parts["depi"] or 1) - 0.172 * (parts["sgai"] or 1)
         + 4.679 * (parts["tata"] or 0) - 0.327 * (parts["lvgi"] or 1))
    return {"m_score": round(m, 3),
            "flagged": bool(m > -1.78),
            "note": "statistical screen on filed accounts; "
                    "> -1.78 warrants review, not an accusation",
            "components": {k: (round(v, 4) if v is not None else None)
                           for k, v in parts.items()}}


# ---- orchestrator -------------------------------------------------------------------

def compute_fundamentals(doc: dict, price: float | None,
                         bench_note: str = "") -> dict:
    block = pick_block(doc)
    if not block:
        return {"methodology_version": FUND_VERSION, "available": False}
    annuals = _annual_series(block)
    ttm_raw = block.get("ttm") or {}
    # Balance-sheet fields live on the annual records (TTM carries only
    # income/cashflow sums): merge so ratio families see a full record.
    bs_latest = _bs_of(annuals[-1] if annuals else {})
    ttm = {**bs_latest, **ttm_raw}
    latest_bs = doc.get("_latest_balance_sheet") or bs_latest
    shares = shares_outstanding(ttm, annuals,
                                _quarter_series(block))
    ps = per_share_base(price, ttm, latest_bs, shares)
    if not ps.get("eps") and shares and _f(ttm.get("pat")):
        # EPS bridge inverse: filings that omit printed EPS still define it
        ps["eps"] = round(_f(ttm.get("pat")) * 1e7 / shares, 2)
        ps["eps_derived"] = True
    ps["_ebitda"] = _f(ttm.get("ebitda")) or derive_ebitda(ttm)
    g = growth(annuals)
    ps["_rev_cagr"] = g.get("revenue_cagr_3y")

    prev_annual = annuals[-2] if len(annuals) >= 2 else None
    payload = {
        "methodology_version": FUND_VERSION,
        "available": bool(ttm or annuals),
        "as_of": (annuals[-1].get("period_end") if annuals
                  else (ttm.get("window_end") if ttm else None)),
        "basis": "consolidated" if doc.get("consolidated") else "standalone",
        "accounting_standard": doc.get("validation", {}).get("confidence"),
        "shares_outstanding_cr": (round(shares / 1e7, 3)
                                  if shares else None),
        "per_share": {k: v for k, v in ps.items() if not k.startswith("_")},
        "profitability": profitability(ttm, prev_annual),
        "dupont": dupont(ttm, prev_annual),
        "liquidity": liquidity(ttm),
        "leverage": leverage(ttm),
        "efficiency": efficiency(ttm),
        "cashflow_quality": cashflow_quality(ttm),
        "growth": g,
        "valuation": valuation(ps),
        "piotroski_f": piotroski_f(annuals[-1], prev_annual)
        if annuals else None,
        "altman_z": altman_z({**ttm, **latest_bs},
                             ps.get("market_cap_cr")),
        "beneish_m": beneish_m(annuals[-1], prev_annual)
        if len(annuals) >= 2 else None,
        "quarter_count": len(_quarter_series(block)),
        "annual_count": len(annuals),
    }
    if bench_note:
        payload["benchmark_note"] = bench_note
    return payload


def _bs_of(rec: dict) -> dict:
    """Balance-sheet items live on the same annual record."""
    keys = ("total_equity", "total_debt", "cash_equivalents",
            "total_assets", "reserves_surplus", "total_current_assets",
            "total_current_liabilities", "inventory", "trade_receivables",
            "trade_payables", "borrowings_non_current",
            "borrowings_current", "ppe_net")
    return {k: rec[k] for k in keys if k in rec}
