"""[F&O-v1] Derivative-aware parsing, classification and TER id-index tests.

Covers:
* two-pass column mapping (Derivative/Unhedged % columns survive separately),
* section capture from the ISIN column (HDFC-style layouts) + Grand-Total NAV,
* the adaptive derivative-disclosure block scanner incl. totals-absent files,
* read-time hedged-sleeve split in WebDB.scheme_holdings / portfolio_analysis,
* the AMFI-export TER id-first index (reported vs computed, fake-zero -> NULL).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import webapp.db as wdb
from src.excel_parser import (
    _is_derivative_sheet,
    _map_columns,
    _num,
    parse_excel,
)

ROOT = Path(__file__).resolve().parent.parent
# CI consumes the committed fixture copy; local dev may shadow it with the
# freshly downloaded original at the repo root.
_SAMPLE_CANDIDATES = [
    ROOT / "tests" / "fixtures" / "Monthly HDFC Equity Savings Fund - 31 July 2026.xlsx",
    ROOT / "Monthly HDFC Equity Savings Fund - 31 July 2026.xlsx",
]
SAMPLE_XLSX = next((p for p in _SAMPLE_CANDIDATES if p.exists()), _SAMPLE_CANDIDATES[0])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _colmap(headers):
    return _map_columns([h.strip().lower() for h in headers])


def _scheme(result, key_sub: str = ""):
    schemes = result["schemes"]
    for name, data in schemes.items():
        if key_sub.lower() in name.lower():
            return name, data
    return next(iter(schemes.items()))


@pytest.fixture(scope="module")
def parsed_sample():
    if not SAMPLE_XLSX.exists():
        pytest.skip("sample HDFC equity-savings xlsx not present")
    return parse_excel(SAMPLE_XLSX)


# ---------------------------------------------------------------------------
# column mapping & primitives
# ---------------------------------------------------------------------------

def test_map_columns_keeps_hedge_columns_separate():
    cm = _colmap(["ISIN", "Coupon (%)", "Name Of the Instrument", "Industry+/Rating",
                  "Quantity", "Market/ Fair Value (Rs. Lacs.)", "% to NAV",
                  "Yield", "~YTC (AT1/Tier 2)", "Derivative\n% to NAV",
                  "Unhedged\n% to NAV"])
    assert len(cm["percent_nav"]) == 1
    assert len(cm["derivative_pct_nav"]) == 1
    assert len(cm["unhedged_pct_nav"]) == 1
    assert len(cm["coupon"]) == 1
    # all five percent-ish/yield-ish columns map to distinct indices
    idxs = {cm[k][0] for k in ("percent_nav", "derivative_pct_nav",
                               "unhedged_pct_nav", "coupon")}
    assert len(idxs) == 4


def test_num_paren_negative_and_percent():
    assert _num("(1,234.5)") == -1234.5
    assert _num("7.18%") == 7.18
    assert _num("—") is None


def test_is_derivative_sheet_trigger(tmp_path):
    df = pd.DataFrame([["DERIVATIVE DISCLOSURE - X"], ["A. Hedging Positions through Futures"]])
    assert _is_derivative_sheet("Anything", df)
    assert _is_derivative_sheet("DerivativeXYZ", pd.DataFrame([["junk"]]))


# ---------------------------------------------------------------------------
# main-sheet hedge columns + sections + grand total
# ---------------------------------------------------------------------------

def test_sample_holdings_carry_hedge_columns(parsed_sample):
    _, s = _scheme(parsed_sample, "HDFCMY")
    hs = s["holdings"]
    equities = [h for h in hs if h.get("derivative_pct_nav") not in ("", None)]
    assert len(equities) >= 60

    def _sum_pct(xs, k):
        return sum(float(x[k]) for x in xs
                   if x.get(k) not in ("", None))

    assert abs(_sum_pct(equities, "derivative_pct_nav") - 29.55) < 0.05
    assert abs(_sum_pct(equities, "unhedged_pct_nav") - 37.91) < 0.05
    # gross weights preserved untouched on the same rows
    gross = sum(float(h["percent_nav"]) for h in hs if h.get("isin", "").startswith("INE"))
    assert gross > 60

    goi = [h for h in hs if str(h.get("company", "")).startswith("7.")]
    assert goi and all(h.get("coupon") for h in goi)


def test_sample_sections_and_grand_total(parsed_sample):
    _, s = _scheme(parsed_sample, "HDFCMY")
    secs = {h.get("section") or "" for h in s["holdings"]}
    low = " | ".join(secs).lower()
    for token in ("equity", "debt instrument", "government securities",
                  "non-convertible", "treps",
                  "net current assets", "certificate of deposit"):
        assert token in low, token
    nav = s.get("nav_lacs")
    assert nav and abs(nav - 569890.70) < 1.0


# ---------------------------------------------------------------------------
# derivative-disclosure sheets
# ---------------------------------------------------------------------------

def test_derivative_parts_bound_to_scheme(parsed_sample):
    _, s = _scheme(parsed_sample, "HDFCMY")
    deriv = s["derivatives"]
    kinds = {(p["kind"], p["hedging"]) for p in deriv["parts"]}
    assert ("futures", True) in kinds          # table A populated
    assert ("swaps", True) in kinds            # table E populated
    futures = next(p for p in deriv["parts"] if p["kind"] == "futures")
    assert futures["computed_totals"]["n_positions"] == 30
    mv = futures["computed_totals"]["market_value_lacs"]
    assert abs(mv - (-168497.08)) < 0.5        # matches sheet subtotal exactly

    pct = s["derivatives_pct_nav"]
    assert pct["reported"] is not None and abs(pct["reported"] + 29.5666) < 0.01
    assert pct["computed"] is not None and abs(pct["computed"] + 29.5666) < 0.01


def test_totals_absent_still_computes(tmp_path):
    """Robustness contract: no footer totals at all -> computed-only output."""
    xlsx = tmp_path / "DerivMini.xlsx"
    rows = [
        ["DERIVATIVE DISCLOSURE - Mini"],
        ["A. Hedging Positions through Futures as on Jan 31, 2026 :"],
        ["Underlying", "Industry", "Long / (Short)", "Futures Price when purchased",
         "Current price of the contract", "Market value\n(Rs. in Lakhs)"],
        ["Alpha Ltd.", "Banks", -100, 900.0, 910.0, -10.0],
        ["Beta Ltd.", "Power", -200, 50.0, 52.0, "-4.4"],
        [],
        ["B. Other than Hedging Positions through Options as on Jan 31, 2026. : Nil"],
    ]
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xl:
        pd.DataFrame(rows).to_excel(xl, sheet_name="Sheet1",
                                    header=False, index=False)
    result = parse_excel(xlsx)
    unbound = result.get("derivatives_unbound", {})
    parts = list(unbound.values())[0]["parts"]
    fut = next(p for p in parts if p["kind"] == "futures")
    assert fut["nil"] is False
    assert fut["computed_totals"]["n_positions"] == 2
    assert abs(fut["computed_totals"]["market_value_lacs"] - (-14.4)) < 1e-9
    opts = next(p for p in parts if p["kind"] == "options")
    assert opts["nil"] is True and opts["positions"] == []


# ---------------------------------------------------------------------------
# TER id-index
# ---------------------------------------------------------------------------

def test_clean_ter_value_fake_zero_is_absent():
    assert wdb._clean_ter_value(0.0) is None
    assert wdb._clean_ter_value("0.00") is None
    assert wdb._clean_ter_value(1.27) == 1.27


# ---------------------------------------------------------------------------
# read-time split (sandbox db, mirrors tests/conftest.py pattern)
# ---------------------------------------------------------------------------

def test_scheme_holdings_splits_hedged_sleeve(tmp_path):
    from webapp.db import WebDB, _create_schema

    con = sqlite3.connect(tmp_path / "t.db")
    cur = con.cursor()
    _create_schema(cur)
    cur.execute("INSERT INTO securities(isin,name,aliases,source_count,"
                "confirmed_equity,cap,sector) VALUES ('INE000000010','Acme Ltd','',2,1,'large','Banks')")
    cur.execute("""INSERT INTO schemes(key,amc,fund_name,source,coverage)
                VALUES ('k','AMC','Hedge Test Fund','amc_website','has_holdings')""")
    rows = [
        # company, isin, gross %nav, hedged, unhedged, section, asset_class
        ("Acme Ltd", "INE000000010", 7.13, 4.33, 2.82, "Equity", "stocks"),
        ("Debt Bond", "", 20.00, None, None, "Debt Instruments", "debt"),
        ("TREPS", "", 30.00, None, None, "Net Current Assets", "cash_equivalents"),
    ]
    for i, (comp, isin, gross, hd, unh, sec, ac) in enumerate(rows):
        cur.execute("""INSERT INTO holdings(scheme_id,amc,fund_name,company,isin,
                    percent_nav,pct_nav_hedged,pct_nav_unhedged,section,
                    asset_class,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (1, "AMC", "Hedge Test Fund", comp, isin, gross, hd, unh,
                     sec, ac, "amc_website"))
    con.commit()

    class _W(WebDB):
        def __init__(self):
            self.path = tmp_path / "t.db"
            self.con = con
            self.con.row_factory = sqlite3.Row

        def _equity_isin_set(self):  # noqa: D102 - sandbox override
            return set()

    w = _W()
    hs = w.scheme_holdings(1)
    fo = [h for h in hs if h["asset_class"] == "future_options"]
    stocks = [h for h in hs if h["asset_class"] == "stocks"]
    assert len(stocks) == 1
    assert stocks[0]["percent_nav"] == pytest.approx(2.82, abs=1e-6)
    assert stocks[0]["percent_nav_raw"] == pytest.approx(7.13, abs=1e-6)
    assert len(fo) == 1 and fo[0]["derived_row"]
    assert fo[0]["percent_nav"] == pytest.approx(4.33, abs=1e-6)
    # allocations still sum identically pre/post split (net 2.82 + hedge 4.33
    # together replace the single gross 7.13 row).
    total = sum(float(h["percent_nav"]) for h in hs)
    assert total == pytest.approx(2.82 + 4.33 + 20 + 30, abs=1e-6)


def test_classify_asset_future_options_paths():
    assert wdb.classify_asset("Futures & Options", "NIFTY AUG FUT", "") == "future_options"
    assert wdb.classify_asset("derivatives", "x", "ABC26") == "future_options"
    assert wdb.classify_asset("", "anything", "ABC26") == "future_options"


def test_resolve_scheme_ter_prefers_ids_then_names(monkeypatch):
    monkeypatch.setattr(wdb, "_navall_fund_key",
                        lambda n: (n or "").lower().replace(" ", ""))

    idx_ids = {"by_isin": {"INX000000001": {"regular": 2.17, "direct": 1.27}},
               "by_amfi": {}, "by_key": {}, "month": "t"}
    navall = {"xfund": {"amfi_regular": "123456", "amfi_direct": "123456",
                        "isin_regular": "INX000000001",
                        "isin_direct": "INX000000001"}}
    rec = wdb._resolve_scheme_ter_record("X Fund", navall, idx_ids)
    assert rec and rec["direct"] == 1.27

    # amfi-code fallback when isins absent from the index
    idx_amfi = {"by_isin": {}, "by_amfi": {"123456": {"regular": 0.5, "direct": None}},
                "by_key": {}, "month": "t"}
    rec2 = wdb._resolve_scheme_ter_record("X Fund", navall, idx_amfi)
    assert rec2 and rec2["regular"] == 0.5 and rec2["direct"] is None

    # name-key hit without any ids at all
    idx_names = {"by_isin": {}, "by_amfi": {},
                 "by_key": {"plainfund": {"regular": 1.0, "direct": 0.75}},
                 "month": "t"}
    rec3 = wdb._resolve_scheme_ter_record("Plain Fund", {}, idx_names)
    assert rec3 and rec3["regular"] == 1.0


def test_infer_category_equity_savings_is_hybrid():
    assert wdb._infer_category("HDFC Equity Savings Fund") == "Hybrid"
    assert wdb._infer_category("ICICI Pru Equity Savings Fund") == "Hybrid"
    assert wdb._infer_category("Multi Asset Allocation Fund") == "Hybrid"
    # name without any known category token keeps legacy fallback
    assert wdb._infer_category("Bluechip Fund") == "Other"


def test_section_is_equity_excludes_derivatives():
    assert not wdb._section_is_equity("Derivatives")
    assert not wdb._section_is_equity("Futures & Options")
    assert wdb._section_is_equity("Equity")


def test_parsed_json_persists_derivative_detail():
    """Provenance contract: full derivative detail survives into the saved JSON."""
    if not SAMPLE_XLSX.exists():
        pytest.skip("sample not present")
    outdir = ROOT / "data" / "parsed" / "amc_websites" / "HDFC_Mutual_Fund" / "2026" / "07"
    jf = outdir / "Monthly HDFC Equity Savings Fund - 31 July 2026.json"
    if jf.exists():
        doc = json.loads(jf.read_text(encoding="utf-8"))
        payload = doc["schemes"].get("HDFCMY") or {}
        assert payload.get("derivatives_pct_nav")
        parts = payload["derivatives"]["parts"]
        assert any(p["kind"] == "swaps" and p["positions"] for p in parts)
        assert any(p["kind"] == "futures" and p["positions"] for p in parts)
