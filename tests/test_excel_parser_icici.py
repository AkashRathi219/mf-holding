"""Regression tests for the ICICI monthly-disclosure parse defects.

Three defects surfaced when ICICI's real monthly ZIPs (per-scheme XLSX from
apimf + /blob) were parsed instead of the undated factsheets the SPA scrape
had been returning:

1. a column-header row could win the fund-name fallback, creating a scheme
   literally named "Company/Issuer/Instrument Name";
2. ``date`` stayed as prose ("Portfolio as on Aug 31,2026"), so the loader
   stored an empty scheme as_of;
3. aggregate section rows ("Total Net Assets", "Equity & Equity Related
   Instruments (Note -1)") leaked into ``holdings``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.excel_parser import (  # noqa: E402
    _is_section_label,
    _norm_as_of,
    _parse_sheet,
)

ICICI_HEADER_ROWS = [
    [None, "ICICI Prudential Mutual Fund", None, None, None, None, None, None],
    [None, "ICICI Prudential Multi Asset Allocation Fund", None, None, None, None, None, None],
    [None, "Portfolio as on Aug 31,2026", None, None, None, None, None, None],
    [None, "Company/Issuer/Instrument Name", "ISIN", "Industry/Rating",
     "Quantity", "Exposure/Market Value(Rs.Lakhs)", "% to Nav", None],
]
ICICI_HOLDING_ROWS = [
    [None, "HDFC Bank Ltd.", "INE040A01034", "Banks", 69199542, 490624.75, 0.0558],
    [None, "ICICI Bank Ltd.", "INE090A01021", "Banks", 22730775, 330505.47, 0.0376],
]


def _icici_frame() -> pd.DataFrame:
    return pd.DataFrame(ICICI_HEADER_ROWS + ICICI_HOLDING_ROWS)


# --------------------------------------------------------------- as_of dates
@pytest.mark.parametrize("raw,expected", [
    ("Portfolio as on Aug 31,2026", "2026-08-31"),
    ("Portfolio as on 31 August 2026", "2026-08-31"),
    ("Portfolio as on 31-Aug-2026", "2026-08-31"),
    ("Portfolio as on July 31, 2026", "2026-07-31"),
    ("Portfolio as on Jun 30,2026", "2026-06-30"),
    ("Portfolio as on 31/Dec/2025", "2025-12-31"),
    ("as on 30.09.2026", "2026-09-30"),
    ("2026-08-31", "2026-08-31"),
])
def test_norm_as_of_recognises_common_layouts(raw, expected):
    assert _norm_as_of(raw) == expected


def test_norm_as_of_leaves_unparseable_text_alone():
    """No date -> unchanged, so nothing is silently blanked."""
    assert _norm_as_of("Portfolio as on date") == "Portfolio as on date"
    assert _norm_as_of("") == ""


# --------------------------------------------------- section-label detection
@pytest.mark.parametrize("label", [
    "Total Net Assets",
    "total net assets",
    "Grand Total",
    "Units of Mutual Fund",
    "Equity & Equity Related Instruments (Note -1)",
    "Equity & Equity Related Instruments",
    "Debt Instruments",
])
def test_aggregate_rows_are_section_labels(label):
    assert _is_section_label(label) is True


@pytest.mark.parametrize("label", [
    "HDFC Bank Ltd.",
    "Life Insurance Corporation of India",
    "ICICI Bank Ltd.",
    "Nifty 50 Index Fund",
])
def test_real_instruments_are_not_section_labels(label):
    assert _is_section_label(label) is False


# --------------------------------------------------------- end-to-end sheet
def test_icici_sheet_drops_aggregate_rows_and_normalises_date():
    res = _parse_sheet(_icici_frame(), "MULTI")
    assert res["fund_name"] == "ICICI Prudential Multi Asset Allocation Fund"
    assert res["date"] == "2026-08-31"

    companies = [h.get("company") for h in res["holdings"]]
    assert "HDFC Bank Ltd." in companies
    assert "ICICI Bank Ltd." in companies
    for junk in ("Total Net Assets",
                 "Equity & Equity Related Instruments (Note -1)",
                 "Units of Mutual Fund"):
        assert junk not in companies


def test_header_row_is_never_used_as_fund_name():
    """The header cell must not win the fund-name fallback."""
    from src.excel_parser import _extract_metadata

    # _parse_sheet pre-seeds these keys; _extract_metadata writes into them.
    res: dict = {"fund_name": "", "date": ""}
    _extract_metadata(_icici_frame(), res)
    assert res.get("fund_name") != "Company/Issuer/Instrument Name"
    assert res.get("fund_name") == "ICICI Prudential Multi Asset Allocation Fund"


def test_holdings_weights_survive_parsing():
    res = _parse_sheet(_icici_frame(), "MULTI")
    by_company = {h["company"]: h for h in res["holdings"]}
    assert float(by_company["HDFC Bank Ltd."]["percent_nav"]) == pytest.approx(0.0558)
    assert by_company["HDFC Bank Ltd."]["isin"] == "INE040A01034"