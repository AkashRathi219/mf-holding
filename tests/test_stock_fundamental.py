"""fund-v1.0.0: fundamental engine — hand-computed anchors.

Synthetic consolidated doc (all Rs crore):
  TTM rev 460, PAT 46, EPS 24.5 -> shares = 46e7/24.5 = 1.8776 cr
  price 245 -> mcap 460 cr, PE 10, net margin 10%
  equity avg(230 TTM, 200 FY25) = 215 -> ROE 21.40%
  EBIT = pbt 69 + fin 14 = 83; invested = debt 135 + eq 230 = 365
  tax rate = 23/69 -> ROIC = 83*(1-1/3)/365 = 15.12%
  FCF = cfo 62 - capex 30 = 32; OCF/PAT = 62/46 = 1.35
"""

from __future__ import annotations

import pytest

from webapp.stock_fundamental import (
    altman_z, beneish_m, build_annual_table, cashflow_quality,
    compute_fundamentals, dupont, efficiency, FUND_VERSION, growth, leverage,
    liquidity, per_share_base, piotroski_f, profitability, shares_outstanding,
    valuation)


def _ttm():
    return {
        "revenue_from_operations": 460.0, "pat": 46.0, "eps_basic": 24.5,
        "ebitda": 110.0, "pbt": 69.0, "tax_expense": 23.0,
        "finance_costs": 14.0, "depreciation_amortisation": 23.0,
        "cfo": 62.0, "capex": 30.0, "dps": 9.0,
        "total_equity": 230.0, "total_assets": 530.0, "total_debt": 135.0,
        "cash_equivalents": 65.0, "reserves_surplus": 175.0,
        "total_current_assets": 195.0, "total_current_liabilities": 105.0,
        "inventory": 68.0, "trade_receivables": 90.0, "trade_payables": 74.0,
        "cost_of_materials": 276.0, "other_expenses": 69.0,
    }


def _annual(fy, rev, pat, eq=200.0):
    return {"period_end": f"20{fy[2:]}-03-31", "fy": fy,
            "revenue_from_operations": rev, "pat": pat, "eps_basic": pat / 2,
            "total_equity": eq, "total_assets": 500.0, "total_debt": 150.0,
            "cash_equivalents": 50.0, "reserves_surplus": 150.0,
            "total_current_assets": 180.0, "total_current_liabilities": 120.0,
            "inventory": 60.0, "trade_receivables": 80.0,
            "trade_payables": 70.0, "cost_of_materials": rev * 0.6,
            "other_expenses": rev * 0.15, "cfo": pat + 15.0, "capex": 25.0}


def _doc():
    ttm = _ttm()
    return {"consolidated": {
        "quarters": [
            {"period_end": f"2025-{m:02d}-30", "kind": "Q",
             "cumulative": False} for m in (9, 12)],
        "annual": [_annual("FY25", 400.0, 40.0),
                   _annual("FY26", 440.0, 44.0, eq=220.0)],
        "ttm": ttm},
        "validation": {"confidence": 100}}


# ---- shares / per-share ---------------------------------------------------------

def test_shares_via_eps_bridge():
    assert shares_outstanding(_ttm(), []) == pytest.approx(46e7 / 24.5)


def test_shares_null_without_eps():
    ttm = _ttm()
    ttm.pop("eps_basic")
    assert shares_outstanding(ttm, []) is None


def test_per_share_base_hand_computed():
    ps = per_share_base(245.0, _ttm(), _ttm(), 46e7 / 24.5)
    assert ps["market_cap_cr"] == pytest.approx(460.0)
    assert ps["bvps"] == pytest.approx(round(230e7 / (46e7 / 24.5), 2))
    assert ps["ev_cr"] == pytest.approx(460.0 + (135.0 - 65.0))


# ---- ratio families ---------------------------------------------------------------

def test_profitability_hand_computed():
    out = profitability(_ttm(), None)
    assert out["net_margin_pct"] == pytest.approx(10.0)
    assert out["ebitda_margin_pct"] == pytest.approx(
        round(110.0 / 460.0 * 100, 2))
    # ROE uses avg(equity_now, equity_prev) when prev supplied
    out_avg = profitability(_ttm(), _annual("FY25", 400, 40))
    assert out_avg["roe_pct"] == pytest.approx(
        round(46.0 / ((230.0 + 200.0) / 2) * 100, 2))


def test_roic_hand_computed():
    out = profitability(_ttm(), None)
    ebit = 69.0 + 14.0
    invested = 135.0 + 230.0
    tax_rate = 23.0 / 69.0
    assert out["roic_pct"] == pytest.approx(
        round(ebit * (1 - tax_rate) / invested * 100, 2))


def test_dupont_3way_multiplies_to_roe():
    d = dupont(_ttm(), None)
    assert d is not None
    # components are emitted pre-rounded; identity holds to rounding error
    assert d["npm"] * d["asset_turnover"] * d["equity_multiplier"] == \
        pytest.approx(d["roe_check"], abs=0.005)
    assert "roe_5way" in d


def test_leverage_and_coverage():
    lev = leverage(_ttm())
    assert lev["debt_to_equity"] == pytest.approx(round(135 / 230, 2))
    assert lev["interest_coverage"] == pytest.approx((69.0 + 14.0) / 14.0, abs=0.1)
    assert lev["net_debt_to_ebitda"] == pytest.approx(round(70 / 110, 2))


def test_liquidity_ratios():
    liq = liquidity(_ttm())
    assert liq["current_ratio"] == pytest.approx(round(195 / 105, 2))
    assert liq["quick_ratio"] == pytest.approx(round((195 - 68) / 105, 2))


def test_efficiency_ccc_identity():
    eff = efficiency(_ttm())
    assert eff["inventory_days"] + eff["receivable_days"] - \
        eff["payable_days"] == pytest.approx(eff["cash_conversion_cycle"])


def test_cashflow_quality_fcf():
    cf = cashflow_quality(_ttm())
    assert cf["fcf_cr"] == pytest.approx(32.0)
    assert cf["ocf_to_pat"] == pytest.approx(round(62 / 46, 2))


def test_growth_yoy_and_cagr():
    g = growth([_annual("FY24", 360.0, 36.0),
                _annual("FY25", 400.0, 40.0),
                _annual("FY26", 440.0, 44.0)])
    assert g["revenue_yoy"] == pytest.approx(0.10)
    assert g["revenue_cagr_3y"] == pytest.approx(
        round((440 / 360) ** (1 / 2) - 1, 4), rel=1e-6)
    assert g["positive_revenue_years"] == 1.0


def test_valuation_pe_pb_and_graham():
    ps = per_share_base(245.0, _ttm(), _ttm(), 46e7 / 24.5)
    ps["_rev_cagr"] = 0.10
    v = valuation(ps)
    assert v["pe"] == pytest.approx(10.0)
    assert v["peg"] == pytest.approx(1.0)
    bvps = ps["bvps"]
    assert v["graham_number"] == pytest.approx(
        round((22.5 * bvps * 24.5) ** 0.5, 2))
    # negative EPS must not produce a nonsense P/E
    bad = dict(ps); bad["eps"] = -5.0
    assert valuation(bad)["pe"] is None


# ---- composite scores -------------------------------------------------------------

def test_piotroski_perfect_company_scores_high():
    cur = {**_annual("FY26", 440.0, 44.0), "cfo": 60.0}
    prev = {**_annual("FY25", 400.0, 40.0), "cfo": 55.0}
    out = piotroski_f(cur, prev)
    assert out["evaluable"] >= 7
    assert out["score"] >= 6


def test_piotroski_needs_prior_year():
    assert piotroski_f(_annual("FY26", 440.0, 44.0), None) is None


def test_altman_z_safe_zone_for_clean_balance_sheet():
    z = altman_z(_ttm(), market_cap_cr=460.0)
    assert z is not None
    # WC/TA=(195-105)/530=.1698*1.2=.204 ; RE/TA=175/530=.330*1.4=.462 ;
    # EBIT/TA=83/530=.1566*3.3=.517 ; MC/TL=(460)/(530-230)=1.533*0.6=.92 ;
    # S/TA=460/530=.868
    assert z["z"] == pytest.approx(.204 + .462 + .517 + .92 +
                                   round(460 / 530, 4) * 1.0, abs=0.05)
    assert z["zone"] in ("safe", "grey")


def test_beneish_m_stable_company_not_flagged():
    cur = _annual("FY26", 440.0, 44.0)
    prev = _annual("FY25", 400.0, 40.0)
    cur["pbt"], cur["cfo"], cur["total_assets"] = 66.0, 60.0, 520.0
    m = beneish_m(cur, prev)
    assert m is not None
    assert m["flagged"] is False
    assert m["m_score"] < -1.78


# ---- orchestrator -----------------------------------------------------------------

def test_compute_fundamentals_full_payload_shape():
    payload = compute_fundamentals(_doc(), price=245.0)
    assert payload["methodology_version"] == FUND_VERSION
    assert payload["available"] is True
    assert payload["basis"] == "consolidated"
    for family in ("per_share", "profitability", "liquidity", "leverage",
                   "efficiency", "cashflow_quality", "growth", "valuation"):
        assert family in payload
    assert payload["quarter_count"] == 2 and payload["annual_count"] == 2


def test_compute_fundamentals_no_statements_is_honest():
    payload = compute_fundamentals({}, price=None)
    assert payload["available"] is False


# ---- annual statement table [fund-table-v1.1.0] -----------------------------------

# 5 synthetic audited years FY22..FY26: revenue grows 10%/yr, PAT 10%/yr,
# equity 10%/yr, debt falls by 2/yr, capex == PAT so FCF is a flat 5.0,
# and inventory/receivables/payables scale with revenue. Expressions are
# written so every arithmetic identity the engine checks holds exactly.
def _year_annual(fy, rev, idx, **kw):
    pat = rev * 0.1
    pbt = pat / 0.75
    dep, fin = 4.0, 3.0
    equity = 40.0 * (1.1 ** idx)
    rec = {
        "period_end": f"{2022 + idx}-03-31", "fy": fy, "kind": "FY",
        "cumulative": True,
        "revenue_from_operations": rev, "other_income": 2.0,
        "total_income": rev + 2.0,
        "cost_of_materials": rev * 0.6, "employee_benefits": rev * 0.05,
        "finance_costs": fin, "depreciation_amortisation": dep,
        "other_expenses": rev * 0.1, "total_expenses": rev + 2.0 - pbt,
        "ebitda": pbt + dep + fin, "exceptional_items": 0.0,
        "pbt": pbt, "tax_expense": pbt * 0.25, "pat": pat,
        "eps_basic": pat / 2.0, "share_capital": 20.0,
        "reserves_surplus": equity - 20.0, "total_equity": equity,
        "total_debt": 20.0 - 2.0 * idx, "cash_equivalents": 10.0 + idx,
        "inventory": rev * 0.15, "trade_receivables": rev * 0.2,
        "trade_payables": rev * 0.1, "total_current_assets": rev * 0.7,
        "total_current_liabilities": rev * 0.4, "total_assets": rev,
        "total_liabilities": rev - equity,
        "cfo": pat + 5.0, "capex": pat, "cfi": -8.0, "cff": -5.0,
        "net_change_in_cash": 1.0,
    }
    rec.update(kw)
    return rec


def _fy_quarters(quarters, fy, apr_year, rev):
    vals = (0.20, 0.25, 0.25, 0.30)
    ends = [(apr_year, 6), (apr_year, 9), (apr_year, 12), (apr_year + 1, 3)]
    for qi, (frac, (y, m)) in enumerate(zip(vals, ends)):
        quarters.append({
            "period_end": f"{y}-03-31" if m == 3 else f"{y}-{m:02d}-30",
            "fy": fy, "kind": "Q", "quarter": f"Q{qi + 1}",
            "cumulative": False, "revenue_from_operations": round(rev * frac, 4),
        })


def _table_doc():
    annuals, quarters = [], []
    for idx in range(5):
        fy = f"FY{22 + idx}"
        rev = 100.0 * (1.1 ** idx)
        annuals.append(_year_annual(fy, rev, idx))
        _fy_quarters(quarters, fy, 2021 + idx, rev)
    return {"consolidated": {"quarters": quarters, "annual": annuals, "ttm": {}},
            "validation": {"confidence": 100}}


def test_table_columns_are_audited_fiscal_years():
    t = build_annual_table(_table_doc())
    assert t["available"] is True
    assert [y["fy"] for y in t["years"]] == ["FY22", "FY23", "FY24", "FY25", "FY26"]
    assert t["years_n"] == 5
    rev = [c["values"]["revenue_from_operations"] for c in t["columns"]]
    assert rev == pytest.approx([100.0, 110.0, 121.0, 133.1, 146.41])
    assert t["methodology_version"] == FUND_VERSION
    assert t["table_version"] == "fund-table-v1.1.0"


def test_table_per_year_metrics_hand_computed():
    t = build_annual_table(_table_doc())
    c0, c1 = t["columns"][0], t["columns"][1]
    assert c0["metrics"]["net_margin_pct"] == pytest.approx(10.0)
    assert c0["metrics"]["gross_margin_pct"] == pytest.approx(40.0)
    assert c0["metrics"]["current_ratio"] == pytest.approx(1.75)
    assert c0["metrics"]["debt_to_equity"] == pytest.approx(20.0 / 40.0)
    assert c0["metrics"]["roe_pct"] == pytest.approx(round(10.0 / 40.0 * 100, 2))
    assert c1["metrics"]["roe_pct"] == pytest.approx(round(11.0 / ((44.0 + 40.0) / 2) * 100, 2))
    assert c0["metrics"]["fcf_cr"] == pytest.approx(5.0)


def test_table_yoy_cagr_and_multi_year_hand_computed():
    t = build_annual_table(_table_doc())
    yoy = [c["metrics"]["revenue_yoy_pct"] for c in t["columns"]]
    assert yoy == [None, 10.0, 10.0, 10.0, 10.0]
    c = t["multi_year"]["cagr"]
    assert c["revenue"] == {"pct": 10.0, "span": 4}
    assert c["pat"]["pct"] == pytest.approx(10.0)
    assert t["multi_year"]["cumulative_fcf_cr"] == pytest.approx(25.0)
    assert t["multi_year"]["net_debt_change_cr"] == pytest.approx(-12.0)
    assert t["multi_year"]["consistency"]["positive_pat_ratio"] == pytest.approx(1.0)
    assert t["multi_year"]["consistency"]["negative_pat_years"] == []
    assert t["multi_year"]["volatility"]["revenue_growth_std_pp"] == pytest.approx(0.0)


def test_table_trends_and_checks():
    t = build_annual_table(_table_doc())
    trd = t["multi_year"]["trend"]
    assert trd["revenue"] == "improving"
    assert trd["net_margin_pct"] == "flat"
    chk = t["checks"]
    for key in ("assets_equal_equity_plus_liabilities", "ebitda_bridge",
                "annual_matches_quarter_sum", "growth_chain_revenue",
                "margin_identity"):
        assert chk[key] is True
    assert t["warnings"] == []


def test_table_single_year_is_honest():
    doc = _table_doc()
    doc["consolidated"]["annual"] = doc["consolidated"]["annual"][-1:]
    t = build_annual_table(doc)
    assert t["years_n"] == 1
    assert t["columns"][0]["fy"] == "FY26"
    assert t["multi_year"]["cagr"]["revenue"]["pct"] is None
    assert t["multi_year"]["cagr"]["revenue"]["span"] is None
    assert t["multi_year"]["cumulative_fcf_cr"] is None
    assert t["multi_year"]["averages"]["net_margin_pct"] is None
    assert t["multi_year"]["trend"]["revenue"] is None
    assert any("Only 1 fiscal year" in w for w in t["warnings"])


def test_table_missing_cashflow_warns_and_nulls_fcf():
    doc = _table_doc()
    rec = doc["consolidated"]["annual"][-1]
    rec["cfo"], rec["capex"] = None, None
    t = build_annual_table(doc)
    assert t["columns"][-1]["metrics"]["fcf_cr"] is None
    assert t["columns"][-1]["values"]["cfo"] is None
    assert any("Cash-flow items missing" in w for w in t["warnings"])


def test_table_negative_pat_year_flagged():
    doc = _table_doc()
    doc["consolidated"]["annual"][-1]["pat"] = -5.0
    t = build_annual_table(doc)
    assert "FY26" in t["multi_year"]["consistency"]["negative_pat_years"]
    assert t["multi_year"]["consistency"]["positive_pat_ratio"] == pytest.approx(0.8)
    assert any("Loss years" in w for w in t["warnings"])


def test_table_bank_like_doc_has_null_gross_margin():
    doc = _table_doc()
    for rec in doc["consolidated"]["annual"]:
        rec["cost_of_materials"] = None
    t = build_annual_table(doc)
    assert all(c["metrics"]["gross_margin_pct"] is None for c in t["columns"])
    assert all(c["metrics"]["net_margin_pct"] is not None for c in t["columns"])


def test_table_ttm_snapshot_and_standalone_fallback():
    doc = _table_doc()
    doc["consolidated"]["ttm"] = {"revenue_from_operations": 500.0, "pat": 50.0,
                                  "ebitda": 120.0, "cfo": 60.0, "capex": 10.0,
                                  "eps": 5.0, "window_start": "2025-03-31",
                                  "window_end": "2025-12-31"}
    t = build_annual_table(doc)
    assert t["ttm"]["revenue_from_operations"] == 500.0
    assert t["ttm"]["fcf_cr"] == 50.0
    assert t["ttm"]["eps_basic"] == 5.0
    cons = doc.pop("consolidated")
    doc["standalone"] = cons
    t2 = build_annual_table(doc)
    assert t2["available"] is True and t2["basis"] == "standalone"


def test_table_no_statement_is_honest():
    assert build_annual_table({})["available"] is False
    assert build_annual_table(
        {"consolidated": {"annual": [], "quarters": [], "ttm": {}}})["available"] is False
