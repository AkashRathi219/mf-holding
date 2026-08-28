"""Ingestion of worker-downloaded NSE output/ CSVs into statement docs +
the quarterly-unaudited fallback of the webapp annual table. Synthetic
fixtures only — no network, no real data/ tree."""

from __future__ import annotations

import textwrap

import pytest

from src.ingest_output_financials import (parse_latest5q_csv,
                                          merge_doc)
from webapp.stock_fundamental import build_annual_table


@pytest.fixture
def feed_csv(tmp_path):
    """NSE results-comparision dump: amounts in Rs LAKH, four discrete
    quarters + one full-year column for a synthetic stock."""
    p = tmp_path / "nse_financials_latest5q.csv"
    p.write_text(textwrap.dedent("""\
        symbol,from_date,to_date,filing_date,revenue_from_operations,total_income,pbt,tax,net_profit,basic_eps,diluted_eps,face_value
        TESTSYM,01-JUL-2024,30-SEP-2024,14-OCT-2024,1000000,1050000,200000,50000,150000,10.5,10.4,2
        TESTSYM,01-OCT-2024,31-DEC-2024,16-JAN-2025,1100000,1160000,220000,55000,165000,11.6,11.5,2
        TESTSYM,01-JAN-2025,31-MAR-2025,22-APR-2025,1200000,1270000,240000,60000,180000,12.7,12.6,2
        TESTSYM,01-APR-2025,30-JUN-2025,19-JUL-2025,1300000,1380000,260000,65000,195000,13.8,13.7,2
        TESTSYM,01-APR-2024,31-MAR-2025,22-APR-2025,4400000,4680000,860000,215000,645000,45.2,45.0,2
    """), encoding="utf-8")
    return str(p)


@pytest.fixture
def feed_csv_q(tmp_path):
    """Feed with five discrete quarters only (no FY column) — the shape
    the annual-table quarterly fallback has to serve."""
    p = tmp_path / "nse_financials_latest5q_q.csv"
    p.write_text(textwrap.dedent("""\
        symbol,from_date,to_date,filing_date,revenue_from_operations,total_income,pbt,tax,net_profit,basic_eps,diluted_eps,face_value
        TESTSYM,01-JUL-2024,30-SEP-2024,14-OCT-2024,1000000,1050000,200000,50000,150000,10.5,10.4,2
        TESTSYM,01-OCT-2024,31-DEC-2024,16-JAN-2025,1100000,1160000,220000,55000,165000,11.6,11.5,2
        TESTSYM,01-JAN-2025,31-MAR-2025,22-APR-2025,1200000,1270000,240000,60000,180000,12.7,12.6,2
        TESTSYM,01-APR-2025,30-JUN-2025,19-JUL-2025,1300000,1380000,260000,65000,195000,13.8,13.7,2
    """), encoding="utf-8")
    return str(p)


def test_parse_latest5q_units_and_classification(feed_csv):
    quarters, annuals = parse_latest5q_csv(feed_csv)
    # 4 discrete quarters + 1 annual (from 01-APR-2024 span)
    assert len(quarters) == 4 and len(annuals) == 1
    q1 = quarters[0]
    # lakh -> crore: 1000000 lakh = 10000 crore
    assert q1["revenue_from_operations"] == 10000.0
    assert q1["total_income"] == 10500.0
    assert q1["pat"] == 1500.0
    # per-share items stay in rupees, never scaled
    assert q1["eps_basic"] == 10.5
    assert q1["_face_value"] == 2.0
    # canonical keys + period metadata
    assert q1["kind"] == "Q" and q1["quarter"] == "Q2"
    assert q1["fy"] == "FY25"            # quarter ending Sep-2024
    assert q1["period_end"] == "2024-09-30"
    assert q1["_source"] == "nse_results_comparision"
    # annual record classified as FY (Apr-Mar span)
    a = annuals[0]
    assert a["kind"] == "FY" and a["period_end"] == "2025-03-31"
    assert a["revenue_from_operations"] == 44000.0
    # sorted oldest -> newest
    assert [q["period_end"] for q in quarters] == \
        ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"]


def test_parse_latest5q_drops_empty_rows(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text(textwrap.dedent("""\
        symbol,from_date,to_date,filing_date,revenue_from_operations,net_profit
        TESTSYM,01-JUL-2024,30-SEP-2024,14-OCT-2024,,
        TESTSYM,01-OCT-2024,31-DEC-2024,16-JAN-2025,500000,25000
    """), encoding="utf-8")
    quarters, annuals = parse_latest5q_csv(str(p))
    assert not annuals and len(quarters) == 1
    assert quarters[0]["pat"] == 250.0


def test_parse_latest5q_cumulative_ytd_flag(tmp_path):
    p = tmp_path / "ytd.csv"
    p.write_text(textwrap.dedent("""\
        symbol,from_date,to_date,filing_date,revenue_from_operations,net_profit
        TESTSYM,01-APR-2024,30-SEP-2024,14-OCT-2024,2100000,300000
    """), encoding="utf-8")
    quarters, _ = parse_latest5q_csv(str(p))
    assert quarters[0]["cumulative"] is True


def test_merge_doc_creates_and_recomputes_ttm(feed_csv):
    quarters, annuals = parse_latest5q_csv(feed_csv)
    doc, action = merge_doc(None, "TESTSYM", "INE000TEST01", quarters, annuals)
    assert action == "created"
    assert doc["units"] == "crore"
    block = doc["consolidated"]
    assert len(block["quarters"]) == 4 and len(block["annual"]) == 1
    ttm = block["ttm"]
    assert ttm["window_start"] == "2024-09-30"
    assert ttm["window_end"] == "2025-06-30"
    # TTM revenue = sum of four crore-scaled quarters
    assert ttm["revenue_from_operations"] == 46000.0
    assert ttm["pat"] == 6900.0
    # per-share items are never summed — latest reading wins
    assert ttm["eps_basic"] == 13.8


def test_merge_doc_never_clobbers_parsed_records(feed_csv):
    quarters, annuals = parse_latest5q_csv(feed_csv)
    existing = {
        "isin": "INE000TEST01", "symbol": "TESTSYM",
        "consolidated": {"quarters": [{
            "period_end": "2024-12-31", "kind": "Q", "fy": "FY25",
            "quarter": "Q3", "revenue_from_operations": 99999.0,
            "pat": 1111.0,
        }], "annual": []},
    }
    doc, action = merge_doc(existing, "TESTSYM", "INE000TEST01",
                            quarters, annuals)
    assert action == "merged"
    qs = doc["consolidated"]["quarters"]
    assert len(qs) == 4                       # 1 parsed + 3 feed quarters
    kept = next(q for q in qs if q["period_end"] == "2024-12-31")
    assert kept["revenue_from_operations"] == 99999.0   # parsed record wins
    assert kept.get("_source") is None


def test_merge_doc_unchanged_when_all_present(feed_csv):
    quarters, annuals = parse_latest5q_csv(feed_csv)
    doc, _ = merge_doc(None, "TESTSYM", "INE000TEST01", quarters, annuals)
    _, action = merge_doc(doc, "TESTSYM", "INE000TEST01", quarters, annuals)
    assert action == "unchanged"


# ---- annual-table quarterly fallback --------------------------------------------

def _doc_from_feed(feed_csv):
    quarters, annuals = parse_latest5q_csv(feed_csv)
    doc, _ = merge_doc(None, "TESTSYM", "INE000TEST01", quarters, annuals)
    return doc


def test_table_falls_back_to_quarters(feed_csv_q):
    table = build_annual_table(_doc_from_feed(feed_csv_q))
    assert table["available"] is True
    assert table["period"] == "Q"
    assert [c["fy"] for c in table["columns"]] == \
        ["Q2 FY25", "Q3 FY25", "Q4 FY25", "Q1 FY26"]
    # income rows only — balance-sheet/cash-flow groups stay absent
    assert {r["section"] for r in table["rows"]} == {"income"}
    col = table["columns"][-1]
    assert col["values"]["revenue_from_operations"] == 13000.0
    # YoY metrics compare the same quarter last year
    assert col["metrics"]["revenue_yoy_pct"] is None   # no FY25 Q1 parsed
    assert table["multi_year"]["cagr"]["revenue"]["unit"] == "quarters"


def test_table_prefers_fy_column_when_feed_has_one(feed_csv):
    """The feed's own full-year column triggers the audited-FY basis even
    when quarters exist alongside it."""
    table = build_annual_table(_doc_from_feed(feed_csv))
    assert table["period"] == "FY"
    assert [c["fy"] for c in table["columns"]] == ["FY25"]


def test_table_dedupes_duplicate_fy_columns(feed_csv):
    """A feed FY row plus an already-parsed record for the same period must
    render one column, not two."""
    doc = _doc_from_feed(feed_csv)
    doc["consolidated"]["annual"].append({
        "period_end": "2025-03-31", "kind": "FY", "fy": "FY25",
        "revenue_from_operations": 44000.0, "pat": 6450.0,
        "eps_basic": 45.2,
    })
    table = build_annual_table(doc)
    assert table["available"] is True
    assert table["period"] == "FY"
    assert [c["fy"] for c in table["columns"]] == ["FY25"]
    assert {r["section"] for r in table["rows"]} == {
        "income", "balance_sheet", "cash_flow"}


def test_table_unavailable_without_any_records():
    table = build_annual_table({"consolidated": {"quarters": [],
                                                 "annual": []}})
    assert table["available"] is False
