"""Unit tests for src/xbrl_fill [stmt-v1.0.0].

All fixtures are minimal NSE-style plain-XBRL documents written to tmp_path;
no network and no dependency on the real data/raw corpus."""

from pathlib import Path

import pytest

from src import xbrl_fill as xf


NS = 'xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in="http://nse/tax"'


def _ctx(cid: str, body: str) -> str:
    return (f'<xbrli:context id="{cid}"><xbrli:entity>'
            f'<xbrli:identifier scheme="X">C</xbrli:identifier></xbrli:entity>'
            f'<xbrli:period>{body}</xbrli:period></xbrli:context>')


def _fact(tag: str, cid: str, val, unit: str = "INR", extra: str = "") -> str:
    return f'<in:{tag} contextRef="{cid}" unitRef="{unit}" decimals="-5"{extra}>{val}</in:{tag}>'


def _doc(extra_facts: str = "", nature: str = "Consolidated",
         audited: str = "Audited") -> str:
    headers = "".join([
        _fact("Symbol", "I_cur", "TESTCO"),
        _fact("WhetherResultsAreAuditedOrUnaudited", "I_cur", audited),
        _fact("NatureOfReportStandaloneConsolidated", "I_cur", nature),
        _fact("DateOfStartOfFinancialYear", "I_cur", "2024-04-01"),
        _fact("DateOfStartOfReportingPeriod", "D_q", "2024-10-01"),
        _fact("DateOfEndOfReportingPeriod", "D_q", "2024-12-31"),
    ])
    pnl = "".join([
        _fact("RevenueFromOperations", "D_q", "10000000000"),
        _fact("EmployeeBenefitExpense", "D_q", "500000000"),
        _fact("ProfitBeforeTax", "D_q", "2000000000"),
        _fact("TaxExpense", "D_q", "500000000"),
        _fact("ProfitOrLossAttributableToOwnersOfParent", "D_q", "1500000000"),
    ])
    bs = "".join([
        _fact("EquityShareCapital", "I_cur", "1000000000"),
        _fact("ReservesAndSurplus", "I_cur", "40000000000"),
        _fact("Assets", "I_cur", "60000000000"),
        _fact("EquityShareCapital", "I_py", "900000000"),
        _fact("Assets", "I_py", "55000000000"),
    ])
    noise = "".join([
        # dimensioned scenario context -> must be excluded
        _fact("RevenueFromOperations", "D_seg", "9990000000"),
        # ratio unit -> skipped
        _fact("OtherIncome", "D_q", "777", unit="pure"),
    ])
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl {NS}>
{_ctx("I_cur", "<xbrli:instant>2024-12-31</xbrli:instant>")}
{_ctx("I_py", "<xbrli:instant>2023-12-31</xbrli:instant>")}
{_ctx("D_q", "<xbrli:startDate>2024-10-01</xbrli:startDate>"
             "<xbrli:endDate>2024-12-31</xbrli:endDate>")}
{_ctx("D_seg", '<xbrli:startDate>2024-10-01</xbrli:startDate>'
               '<xbrli:endDate>2024-12-31</xbrli:endDate>'
               '<xbrli:scenario><xbrli:member/></xbrli:scenario>')}
{headers}{pnl}{bs}{noise}{extra_facts}
</xbrli:xbrl>'''


def _write(tmp_path: Path, text: str, name: str = "T_1.xml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---- low-level -------------------------------------------------------------------

def test_fact_number_variants():
    assert xf.fact_number(" 1,234.5 ") == 1234.5
    assert xf.fact_number("100", sign="-") == -100.0
    assert xf.fact_number("2", scale="7") == 20_000_000.0
    assert xf.fact_number("-") is None
    assert xf.fact_number("") is None
    assert xf.fact_number(None) is None
    assert xf.fact_number("abc") is None


def test_duration_kind():
    from datetime import date as d
    assert xf.duration_kind(d(2024, 10, 1), d(2024, 12, 31)) == "Q"
    assert xf.duration_kind(d(2024, 4, 1), d(2024, 9, 30)) == "H1"
    assert xf.duration_kind(d(2024, 4, 1), d(2024, 12, 31)) == "9M"
    assert xf.duration_kind(d(2024, 4, 1), d(2025, 3, 31)) == "FY"
    assert xf.duration_kind(d(2024, 12, 31), d(2024, 10, 1)) is None


def test_parse_filing_periods_units_and_segments(tmp_path):
    ff = xf.parse_filing(_write(tmp_path, _doc()))
    assert ff["header"]["Symbol"] == "TESTCO"
    q = ff["periods"][("D", "Q|2024-12-31")]
    assert q["RevenueFromOperations"] == 1000.0            # INR -> crore
    assert q["EmployeeBenefitExpense"] == 50.0
    assert q["ProfitBeforeTax"] == 200.0
    assert q["ProfitOrLossAttributableToOwnersOfParent"] == 150.0
    cur = ff["periods"][("I", "2024-12-31")]
    assert cur["EquityShareCapital"] == 100.0
    assert cur["Assets"] == 6000.0
    assert ("I", "2023-12-31") in ff["periods"]
    all_vals = {round(v, 4) for p in ff["periods"].values()
                for v in p.values()}
    assert 999.0 not in all_vals                           # scenario excluded
    assert 777.0 not in all_vals                           # pure-unit skipped


def test_parse_filing_misdated_context_header_override(tmp_path):
    doc = _doc().replace(
        '<xbrli:context id="D_q"><xbrli:entity><xbrli:identifier scheme="X">'
        'C</xbrli:identifier></xbrli:entity><xbrli:period>'
        '<xbrli:startDate>2024-10-01</xbrli:startDate>',
        '<xbrli:context id="D_q"><xbrli:entity><xbrli:identifier scheme="X">'
        'C</xbrli:identifier></xbrli:entity><xbrli:period>'
        '<xbrli:startDate>2019-01-01</xbrli:startDate>')
    ff = xf.parse_filing(_write(tmp_path, doc))
    assert ("D", "Q|2024-12-31") in ff["periods"]          # headers win


# ---- selection + document ----------------------------------------------------------

def _row(filing_date: str, audited: str, consolidated: str) -> dict:
    return {"seqNumber": "1", "symbol": "TESTCO", "isin": "INE000X0101",
            "audited": audited, "consolidated": consolidated,
            "filingDate": filing_date, "xbrl": ""}


def test_filing_preference_ordering():
    hdr = {}
    assert xf.filing_preference(
        {"audited": "Un-Audited", "consolidated": "Non-Consolidated"}, hdr) \
        == (1, 1)
    assert xf.filing_preference(
        {"audited": "Audited", "consolidated": "Consolidated"}, hdr) == (0, 0)


def test_build_document_shape_and_selection(tmp_path):
    row = _row("15-Jan-2025 18:30", "Audited", "Consolidated")
    path = tmp_path / "TESTCO" / "TESTCO_1.xml"
    path.parent.mkdir(parents=True)
    path.write_text(_doc(), encoding="utf-8")
    doc, stats = xf.prepare_symbol("TESTCO", "INE000X0101", "Test Co",
                                   [row], tmp_path)
    assert stats["parsed_ok"] == 1
    assert doc is not None
    assert doc["schema_version"] == "stmt-v1.0.0"
    cons = doc["consolidated"]
    assert cons["quarters"], "expected at least one quarter record"
    q0 = next(q for q in cons["quarters"] if q["period_end"] == "2024-12-31")
    assert q0["revenue_from_operations"] == 1000.0
    assert isinstance(doc["validation"]["confidence"], int)
    assert doc["_latest_balance_sheet"]["share_capital"] == 100.0
    assert doc["sources"][0]["tier"] == "xbrl"


def test_prepare_symbol_no_canonical_match(tmp_path):
    junk = ('<?xml version="1.0"?>\n<xbrli:xbrl '
            'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:x="urn:x">'
            '<xbrli:context id="A"><xbrli:entity>'
            '<xbrli:identifier scheme="X">C</xbrli:identifier></xbrli:entity>'
            '<xbrli:period><xbrli:instant>2024-12-31</xbrli:instant>'
            '</xbrli:period></xbrli:context>'
            '<x:SomethingUnrelated contextRef="A" unitRef="INR">5</x:SomethingUnrelated>'
            '</xbrli:xbrl>')
    row = _row("15-Jan-2025 18:30", "Audited", "Consolidated")
    jdir = tmp_path / "JUNK"
    jdir.mkdir(parents=True)
    (jdir / "JUNK_1.xml").write_text(junk, encoding="utf-8")
    doc, stats = xf.prepare_symbol("JUNK", "INE000X0202", "Junk Ltd",
                                   [row], tmp_path)
    assert stats["parsed_ok"] == 0
    assert doc is None
    assert stats["status"] == "no_canonical_match"


def test_missing_files_counted():
    stats_probe = xf.prepare_symbol(
        "NOPE", "INE000X0303", "Nope", [_row("15-Jan-2025 18:30",
                                             "Audited", "Consolidated")],
        Path("Z:/nonexistent_dir"))
    doc, stats = stats_probe
    assert doc is None
    assert stats["files_missing"] == 1


def test_diff_documents_threshold():
    def mk(rev):
        return {"consolidated": {"quarters": [
            {"period_end": "2024-12-31", "revenue_from_operations": rev}]}}
    diffs = xf.diff_documents(mk(100.0), mk(112.0))
    assert len(diffs) == 1 and diffs[0]["key"] == "revenue_from_operations"
    assert xf.diff_documents(mk(100.0), mk(103.0)) == []


def test_latest_balance_sheet_prefers_equity_bearing_record():
    recs = [{"period_end": "2024-06-30", "cfo": 10.0},
            {"period_end": "2024-09-30", "total_equity": 500.0,
             "total_debt": 100.0}]
    bs = xf.latest_balance_sheet([], recs)
    assert bs["total_equity"] == 500.0


def test_load_metadata_groups_by_symbol_uppercase(tmp_path):
    meta = {"a": {"seqNumber": "1", "symbol": "ab"}, 
            "b": {"seqNumber": "2", "symbol": "AB"},
            "c": {"seqNumber": "3", "symbol": "cd"}}
    p = tmp_path / "_metadata.json"
    p.write_text(__import__("json").dumps(meta), encoding="utf-8")
    grouped = xf.load_metadata(p)
    assert sorted(grouped.keys()) == ["AB", "CD"]
    assert len(grouped["AB"]) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
