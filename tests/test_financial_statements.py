"""stmt-v1.0.0: statement schema + assembly pipeline — hand-computed anchors.

No network / no AI: every fixture is synthetic and every expected value is
computed by hand in the comments.
"""

from __future__ import annotations

import pytest

from src import statement_schema as ss
from src.financial_statements import (
    _align_columns, assemble, build_section_records, compute_ttm,
    validate_and_score)


# ---- label matching -------------------------------------------------------------

def test_match_label_exact_alias():
    assert ss.match_label("Revenue from operations") == \
        ("revenue_from_operations", 1.0)
    assert ss.is_exact_label("Revenue from operations")
    # bracketed qualifiers are stripped before matching
    assert ss.match_label("Profit/(Loss) Before Tax")[0] == "pbt"


def test_match_label_fuzzy_fallback_and_reject():
    canon, score = ss.match_label("Cost of Materials Consummed")   # typo'd
    assert canon == "cost_of_materials" and score >= 0.72
    canon2, score2 = ss.match_label("xyzzy plugh zorkmid")
    assert canon2 is None


def test_gross_sales_line_no_longer_claims_net_revenue():
    # 'Value of Sales & Services' must NOT hit revenue_from_operations
    # exactly (it is the GROSS line above it in Reliance-style filings)
    canon, score = ss.match_label("Value of Sales & Services (Revenue)")
    if canon == "revenue_from_operations":
        assert score < 1.0 and not ss.is_exact_label(
            "Value of Sales & Services (Revenue)")


# ---- number / unit parsing ------------------------------------------------------

def test_to_number_formats():
    assert ss.to_number("325,290") == 325290.0
    assert ss.to_number("(4,214)") == -4214.0
    assert ss.to_number("12.35") == 12.35
    assert ss.to_number("-") is None
    assert ss.to_number("") is None
    assert ss.to_number("na") is None


def test_unit_scale_detection():
    assert ss.unit_scale_to_crore("(Rs. in Lakhs except per share data)") == 0.10
    assert ss.unit_scale_to_crore("Rs in Crore") == 1.0
    assert ss.unit_scale_to_crore("figures in million") == 10.0
    assert ss.unit_scale_to_crore("no unit note here") == 1.0


# ---- period parsing --------------------------------------------------------------

def test_parse_period_date_variants():
    assert ss.parse_period_date("2026-06-30") == (2026, 6, 30)      # ISO (AI tier)
    assert ss.parse_period_date("30-06-2026") == (2026, 6, 30)
    assert ss.parse_period_date("Jun'26") == (2026, 6, 1)
    assert ss.parse_period_date("30th June, 2026") == (2026, 6, 30)
    assert ss.parse_period_date("quarter ended June 30, 2026") == (2026, 6, 30)
    assert ss.parse_period_date("no date here") is None


def test_classify_period_kinds():
    assert ss.classify_period("Quarter Ended 30 Jun") == "Q"
    assert ss.classify_period("Half Year Ended 30 September") == "H1"
    assert ss.classify_period("Nine Months Ended 31 December") == "9M"
    assert ss.classify_period("Year Ended 31 March (Audited)") == "FY"
    assert ss.classify_period("nothing", default="Q") == "Q"


def test_fiscal_year_indian_apr_mar_boundary():
    assert ss.fiscal_year((2026, 6), "Q") == "FY27"
    assert ss.fiscal_year((2026, 3), "FY") == "FY26"
    assert ss.fiscal_year((2026, 1), "Q") == "FY26"
    assert ss.fiscal_year((2025, 12), "9M") == "FY26"


def test_quarter_of_month():
    assert ss.quarter_of_month(6, "Q") == "Q1"
    assert ss.quarter_of_month(9, "Q") == "Q2"     # Sep QUARTER column = Q2
    assert ss.quarter_of_month(12, "Q") == "Q3"
    assert ss.quarter_of_month(3, "Q") == "Q4"
    assert ss.quarter_of_month(9, "H1") is None    # cumulative has no quarter
    assert ss.quarter_of_month(13, "Q") is None


# ---- identity validation ---------------------------------------------------------

def test_validate_identity_assets_equal_equity_plus_liabilities():
    good = {"total_assets": 1000.0, "total_equity": 400.0,
            "total_liabilities": 600.0}
    assert ss.validate_statement(good) == []
    bad = {"total_assets": 1000.0, "total_equity": 400.0,
           "total_liabilities": 500.0}
    issues = ss.validate_statement(bad)
    assert any(i.startswith("identity_assets_ne") for i in issues)


def test_validate_sanity_flags():
    assert any("revenue_negative" in i for i in
               ss.validate_statement({"revenue_from_operations": -5.0}))
    wild = {"pat": 300.0, "pbt": 10.0}
    assert any("pat_exceeds_pbt" in i for i in ss.validate_statement(wild))
    sane = {"pat": 8.0, "pbt": 10.0}
    assert ss.validate_statement(sane) == []


def test_derive_ebitda_hand_computed():
    rec = {"total_income": 1000.0, "total_expenses": 800.0,
           "depreciation_amortisation": 50.0, "finance_costs": 30.0,
           "exceptional_items": 10.0}
    out = ss.derive_ebitda(rec)
    # ebitda = 1000 - 800 + 50 + 30 - 10 = 270
    assert out["ebitda"] == pytest.approx(270.0)


def test_derive_total_debt_and_liabilities():
    out = ss.derive_ebitda({"borrowings_non_current": 300.0,
                            "borrowings_current": 120.0,
                            "total_assets": 900.0, "total_equity": 350.0})
    assert out["total_debt"] == pytest.approx(420.0)
    assert out["total_liabilities"] == pytest.approx(550.0)


# ---- DP column alignment ---------------------------------------------------------

def test_align_columns_monotonic_mapping():
    # two figure columns drift right of three header anchors
    assert _align_columns([395.0, 474.0], [321.0, 403.0, 470.0]) == {0: 1, 1: 2}
    assert _align_columns([100.0], [100.0]) == {0: 0}
    # extra figure columns beyond anchors stay unpaired
    got = _align_columns([200.0, 300.0], [150.0])
    assert got == {0: 0}


# ---- build_section_records -------------------------------------------------------

def test_build_records_prefers_exact_over_fuzzy():
    rows = [
        {"section": "consolidated", "canon": "revenue_from_operations",
         "match_score": 0.82, "exact": False,
         "values": {"Q|2026-6-30": 999.0}},
        {"section": "consolidated", "canon": "revenue_from_operations",
         "match_score": 1.0, "exact": True,
         "values": {"Q|2026-6-30": 311.85}},
    ]
    sec = build_section_records(rows)
    rec = sec["consolidated"][("Q", (2026, 6, 30))]
    assert rec["revenue_from_operations"] == pytest.approx(311.85)


def _q(kind, y, m, d, vals):
    return {(kind, (y, m, d)): dict(vals)}


# ---- assemble: discrete-quarter chains -------------------------------------------

def test_assemble_derives_q2_q3_q4_from_cumulative_chain():
    smap = {}
    smap.update(_q("Q", 2025, 6, 30, {
        "revenue_from_operations": 100.0, "pat": 10.0}))          # Q1 actual
    smap.update(_q("H1", 2025, 9, 30, {
        "revenue_from_operations": 220.0, "pat": 22.0}))          # H1 cum
    smap.update(_q("9M", 2025, 12, 31, {
        "revenue_from_operations": 360.0, "pat": 36.0}))          # 9M cum
    smap.update(_q("FY", 2026, 3, 31, {
        "revenue_from_operations": 520.0, "pat": 52.0}))          # audited FY
    quarters, annuals = assemble(smap)

    by_end = {q["period_end"]: q for q in quarters}
    # H1 - Q1 = Q2 = 120 ; 9M - H1 = Q3 = 140 ; FY - 9M = Q4 = 160
    assert by_end["2025-09-30"]["revenue_from_operations"] == pytest.approx(120.0)
    assert by_end["2025-09-30"]["quarter"] == "Q2"
    assert by_end["2025-09-30"]["derived"] is True
    assert by_end["2025-12-31"]["revenue_from_operations"] == pytest.approx(140.0)
    assert by_end["2025-12-31"]["quarter"] == "Q3"
    assert by_end["2026-03-31"]["revenue_from_operations"] == pytest.approx(160.0)
    assert by_end["2026-03-31"]["quarter"] == "Q4"
    assert not by_end["2025-06-30"].get("derived")   # printed Q1, not derived
    # audited FY lands in the annual list too
    assert len(annuals) == 1
    assert annuals[0]["revenue_from_operations"] == pytest.approx(520.0)
    assert annuals[0]["fy"] == "FY26"


def test_assemble_prefers_actual_quarter_over_derived():
    smap = {}
    smap.update(_q("Q", 2025, 6, 30, {"pat": 10.0}))
    smap.update(_q("H1", 2025, 9, 30, {"pat": 22.0}))
    smap.update(_q("Q", 2025, 9, 30, {"pat": 12.5}))   # actual printed Q2
    quarters, _annuals = assemble(smap)
    q2 = next(q for q in quarters if q["period_end"] == "2025-09-30")
    assert q2["pat"] == pytest.approx(12.5)            # actual wins
    assert not q2.get("derived")


# ---- TTM ------------------------------------------------------------------------

def test_compute_ttm_sums_last_four_discrete_quarters():
    def mk(y, m, rev, pat):
        return {"period_end": f"{y}-{m:02d}-15", "fy": f"FY{y % 100}",
                "kind": "Q", "quarter": "Qx", "cumulative": False,
                "revenue_from_operations": float(rev), "pat": float(pat),
                "eps_basic": 7.5}
    quarters = [mk(2025, 6, 100.0, 10.0), mk(2025, 9, 110.0, 11.0),
                mk(2025, 12, 120.0, 12.0), mk(2026, 3, 130.0, 13.0)]
    ttm = compute_ttm(quarters)
    assert ttm["revenue_from_operations"] == pytest.approx(460.0)
    assert ttm["pat"] == pytest.approx(46.0)
    assert ttm["eps_basic"] == pytest.approx(7.5)     # per-share: latest wins
    assert ttm["window_start"] == "2025-06-15"
    assert ttm["window_end"] == "2026-03-15"


def test_compute_ttm_requires_contiguous_window():
    def mk(y, m):
        return {"period_end": f"{y}-{m:02d}-15", "fy": "FY", "kind": "Q",
                "quarter": "Qx", "cumulative": False,
                "revenue_from_operations": 1.0}
    gappy = [mk(2024, 6), mk(2024, 9), mk(2025, 6), mk(2025, 9)]
    assert compute_ttm(gappy) is None
    assert compute_ttm(gappy[:3]) is None              # <4 quarters


def test_compute_ttm_ignores_cumulative_rows():
    disc = [{"period_end": f"2025-{m:02d}-15", "kind": "Q",
             "cumulative": False, "quarter": "Qx", "fy": "FY",
             "revenue_from_operations": 100.0}
            for m in (6, 9, 12)] + \
        [{"period_end": "2026-03-31", "kind": "H1", "cumulative": True,
          "quarter": None, "fy": "FY", "revenue_from_operations": 9999.0},
         {"period_end": "2026-03-15", "kind": "Q", "cumulative": False,
          "quarter": "Q4", "fy": "FY", "revenue_from_operations": 140.0}]
    ttm = compute_ttm(disc)
    assert ttm is not None
    assert ttm["revenue_from_operations"] == pytest.approx(440.0)


# ---- confidence scoring -----------------------------------------------------------

def test_validate_and_score_gates():
    issues, conf = validate_and_score([], [])
    assert conf == 0 and "no_records" in issues

    rich_q = [{"period_end": "2026-06-30", "fy": "FY27", "kind": "Q",
               "quarter": "Q1", "cumulative": False,
               "revenue_from_operations": 500.0, "pat": 40.0}]
    rich_a = [dict(rich_q[0], period_end="2026-03-31")]
    issues, conf = validate_and_score(rich_q, rich_a)
    assert issues == [] and conf == 100

    poor_q = [{"period_end": "2026-06-30", "fy": "FY27", "kind": "Q",
               "quarter": "Q1", "cumulative": False}]
    issues, conf = validate_and_score(poor_q, [])
    assert conf < 100
    assert any("missing_revenue" in i for i in issues)
