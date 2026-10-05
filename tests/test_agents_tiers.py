"""[T32] Pure completeness-ladder tiering tests (SPEC §11.0 / AC-14).

Covers the four AC-14 cases (full-disclosure 97% -> ``COMPLETE_100``; a
truncated top-10 factsheet at 45% -> ``TOP10_FALLBACK`` and NOT escalated;
full-disclosure 60% -> ``ESCALATE``; Σ>105 or a weight>100 ->
``SUSPECT_SCALE`` + escalated), the strict-rule safety property for
``unknown``/missing document classes (never T1), the ``MISSING`` flag,
weight-key/string parsing, unparseable-row counting, band boundaries and
determinism.
"""

from __future__ import annotations

import json

import pytest

from src.agents.tiers import (
    COMPLETE_MAX,
    COMPLETE_MIN,
    FLAG_MISSING,
    FLAG_SUSPECT_SCALE,
    TOPN_EXPECTED_ROWS,
    TIER_CODES,
    TierResult,
    classify,
)
from src.document_class import (
    FACTSHEET_TOPN,
    FORTNIGHTLY,
    FULL_PORTFOLIO,
    UNKNOWN,
)

TOP10_45 = (10.0, 9.0, 8.0, 5.0, 4.0, 3.0, 3.0, 1.5, 1.0, 0.5)


def _rows(*weights, key="weight_pct"):
    return [{key: w, "instrument": f"H{i}"} for i, w in enumerate(weights, start=1)]


def test_constants_and_tier_codes():
    assert COMPLETE_MIN == 95.0
    assert COMPLETE_MAX == 105.0
    assert TOPN_EXPECTED_ROWS == 10
    assert TIER_CODES == {
        "COMPLETE_100": "T0",
        "TOP10_FALLBACK": "T1",
        "ESCALATE": "T2",
        "MANUAL": "T3",
    }


# ---------------------------------------------------------------------------
# AC-14 (a): full-disclosure 97% -> COMPLETE_100, not escalated
# ---------------------------------------------------------------------------

def test_full_disclosure_97pct_is_t0_not_escalated():
    result = classify(_rows(50.0, 30.0, 17.0), FULL_PORTFOLIO)
    assert isinstance(result, TierResult)
    assert result.tier == "COMPLETE_100"
    assert result.tier_code == "T0"
    assert result.coverage_pct == 97.0
    assert result.n_holdings == 3
    assert result.document_class == FULL_PORTFOLIO
    assert result.escalate is False
    assert result.flags == ()


# ---------------------------------------------------------------------------
# AC-14 (b): truncated top-10 factsheet at 45% -> TOP10_FALLBACK, NOT escalated
# (critical regression: class-relative judgement, not a failure)
# ---------------------------------------------------------------------------

def test_factsheet_top10_at_45pct_is_t1_not_escalated():
    result = classify(_rows(*TOP10_45), FACTSHEET_TOPN)
    assert result.tier == "TOP10_FALLBACK"
    assert result.tier_code == "T1"
    assert result.coverage_pct == 45.0
    assert result.n_holdings == 10
    assert result.document_class == FACTSHEET_TOPN
    assert result.escalate is False
    assert result.flags == ()


# ---------------------------------------------------------------------------
# AC-14 (c): full-disclosure 60% -> ESCALATE
# ---------------------------------------------------------------------------

def test_full_disclosure_60pct_is_t2_escalated():
    result = classify(_rows(30.0, 20.0, 10.0), FULL_PORTFOLIO)
    assert result.tier == "ESCALATE"
    assert result.tier_code == "T2"
    assert result.coverage_pct == 60.0
    assert result.escalate is True
    assert result.flags == ()


# ---------------------------------------------------------------------------
# AC-14 (d): suspect scale (Σ>105 or a single weight>100) - always escalated
# ---------------------------------------------------------------------------

def test_coverage_130_is_suspect_scale_and_escalated():
    result = classify(_rows(40.0, 30.0, 25.0, 20.0, 15.0), FULL_PORTFOLIO)
    assert FLAG_SUSPECT_SCALE in result.flags
    assert result.escalate is True
    assert result.tier == "ESCALATE"
    assert result.tier_code == "T2"


def test_single_weight_140_sits_on_t0_suspect_and_escalated():
    result = classify(_rows(140.0, -45.0), FULL_PORTFOLIO)
    assert FLAG_SUSPECT_SCALE in result.flags
    assert result.escalate is True
    assert result.coverage_pct == 95.0
    assert result.tier == "COMPLETE_100"
    assert result.tier_code == "T0"


def test_factsheet_weight_over_100_is_t2_suspect():
    result = classify(
        _rows(140.0, 10.0, 10.0, 10.0, 10.0, 5.0, 5.0, 5.0, 5.0, 5.0), FACTSHEET_TOPN)
    assert result.tier == "ESCALATE"
    assert result.tier_code == "T2"
    assert FLAG_SUSPECT_SCALE in result.flags
    assert result.escalate is True


def test_factsheet_coverage_130_all_weights_le_100_sits_on_t1():
    result = classify(_rows(*((15.0,) * 8 + (5.0, 5.0))), FACTSHEET_TOPN)
    assert result.tier == "TOP10_FALLBACK"
    assert result.tier_code == "T1"
    assert FLAG_SUSPECT_SCALE in result.flags
    assert result.escalate is True


# ---------------------------------------------------------------------------
# safety: unknown / missing / unrecognised class is judged strict, never T1
# ---------------------------------------------------------------------------

def test_unknown_class_top10_shape_at_45pct_is_never_t1():
    result = classify(_rows(*TOP10_45), UNKNOWN)
    assert result.tier_code != "T1"
    assert result.tier == "ESCALATE"
    assert result.tier_code == "T2"
    assert result.escalate is True
    assert result.document_class == UNKNOWN


def test_missing_document_class_is_normalised_to_unknown_strict():
    result = classify(_rows(*TOP10_45), None)
    assert result.document_class == UNKNOWN
    assert result.tier == "ESCALATE"
    assert result.escalate is True


def test_unrecognised_document_class_is_normalised_to_unknown():
    result = classify(_rows(25.0, 20.0), "factsheet")
    assert result.document_class == UNKNOWN
    assert result.tier == "ESCALATE"
    assert result.escalate is True


def test_fortnightly_is_judged_by_strict_rule():
    result = classify(_rows(50.0, 30.0, 17.0), FORTNIGHTLY)
    assert result.tier == "COMPLETE_100"
    assert result.escalate is False
    low = classify(_rows(30.0, 20.0, 10.0), FORTNIGHTLY)
    assert low.tier == "ESCALATE"
    assert low.escalate is True


# ---------------------------------------------------------------------------
# MISSING (no rows at all)
# ---------------------------------------------------------------------------

def test_none_holdings_is_t2_missing():
    result = classify(None, FULL_PORTFOLIO)
    assert result.tier == "ESCALATE"
    assert result.tier_code == "T2"
    assert result.flags == (FLAG_MISSING,)
    assert result.escalate is True
    assert result.n_holdings == 0
    assert result.coverage_pct == 0.0
    assert result.document_class == FULL_PORTFOLIO


def test_empty_list_holdings_is_t2_missing():
    result = classify([], FACTSHEET_TOPN)
    assert result.flags == (FLAG_MISSING,)
    assert result.escalate is True
    assert result.n_holdings == 0


def test_single_empty_row_is_not_missing_but_escalates():
    result = classify([{}], FULL_PORTFOLIO)
    assert result.flags == ()
    assert result.n_holdings == 1
    assert result.coverage_pct == 0.0
    assert result.tier == "ESCALATE"
    assert result.escalate is True


# ---------------------------------------------------------------------------
# weight parsing: four keys, floats and "2.35%" strings, unparseable rows
# ---------------------------------------------------------------------------

def test_weight_key_precedence_percent_nav_first():
    result = classify(
        [{"percent_nav": 1.0, "weight_pct": 3.0, "pct": 2.0, "%": 4.0}], FULL_PORTFOLIO)
    assert result.coverage_pct == 1.0
    assert result.n_holdings == 1


def test_weight_key_fallback_when_preferred_key_unparseable():
    result = classify([{"percent_nav": "n/a", "pct": 7.5}], FULL_PORTFOLIO)
    assert result.coverage_pct == 7.5
    assert result.n_holdings == 1


def test_string_percent_and_all_four_keys_parse():
    rows = [
        {"percent_nav": 5.0},
        {"weight_pct": "3.5%"},
        {"pct": 2},
        {"%": " 1.5 % "},
    ]
    result = classify(rows, FULL_PORTFOLIO)
    assert result.coverage_pct == 12.0
    assert result.n_holdings == 4


def test_unparseable_rows_skipped_but_counted():
    rows = [
        {"weight_pct": 50.0},
        {"weight_pct": "n/a"},
        {"weight_pct": None},
        "garbage",
    ]
    result = classify(rows, FULL_PORTFOLIO)
    assert result.n_holdings == 4
    assert result.coverage_pct == 50.0
    assert "3 unparseable row(s) skipped" in result.reason
    assert result.tier == "ESCALATE"
    assert result.escalate is True


def test_factsheet_with_no_parseable_weights_is_t2():
    result = classify([{"weight_pct": "?"} for _ in range(10)], FACTSHEET_TOPN)
    assert result.tier == "ESCALATE"
    assert result.escalate is True
    assert result.n_holdings == 10


def test_factsheet_row_count_outside_band_is_t2():
    result = classify(_rows(*([5.0] * 20)), FACTSHEET_TOPN)
    assert result.tier == "ESCALATE"
    assert result.tier_code == "T2"
    assert result.escalate is True
    assert result.n_holdings == 20


# ---------------------------------------------------------------------------
# band boundaries (95 / 105 inclusive)
# ---------------------------------------------------------------------------

def test_coverage_band_boundaries():
    at_min = classify(_rows(95.0), FULL_PORTFOLIO)
    assert at_min.tier == "COMPLETE_100"
    assert at_min.escalate is False
    at_max = classify(_rows(100.0, 5.0), FULL_PORTFOLIO)
    assert at_max.tier == "COMPLETE_100"
    assert at_max.escalate is False
    assert at_max.flags == ()
    below = classify(_rows(94.9), FULL_PORTFOLIO)
    assert below.tier == "ESCALATE"
    assert below.escalate is True
    above = classify(_rows(100.0, 5.1), FULL_PORTFOLIO)
    assert above.tier == "ESCALATE"
    assert above.escalate is True
    assert FLAG_SUSPECT_SCALE in above.flags


# ---------------------------------------------------------------------------
# determinism + purity
# ---------------------------------------------------------------------------

def test_classify_is_deterministic():
    holdings = _rows(50.0, 30.0, 17.0)
    first = classify(holdings, FULL_PORTFOLIO)
    again = classify(list(holdings), FULL_PORTFOLIO)
    assert again == first


def test_classify_is_deterministic_across_classes():
    cases = [
        (_rows(50.0, 30.0, 17.0), FULL_PORTFOLIO),
        (_rows(*TOP10_45), FACTSHEET_TOPN),
        (None, UNKNOWN),
    ]
    for holdings, dc in cases:
        result = classify(holdings, dc)
        assert result == classify(holdings, dc)
        assert result.tier_code in ("T0", "T1", "T2")


def test_classify_does_not_mutate_holdings():
    holdings = [{"weight_pct": "2.35%"}, {"percent_nav": 3.0}]
    before = json.dumps(holdings, sort_keys=True)
    classify(holdings, FULL_PORTFOLIO)
    assert json.dumps(holdings, sort_keys=True) == before


def test_result_is_frozen():
    result = classify(None, FULL_PORTFOLIO)
    with pytest.raises(AttributeError):
        result.escalate = False
