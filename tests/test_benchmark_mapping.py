"""Benchmark selection v2: index_name precedence + ordered keyword rules.

The rules must resolve real fund names to the Nifty TR index each fund
actually tracks — most-specific-first, so 'Nifty 500' never lands on
'NIFTY 50' and 'Nifty 200 Momentum 30' lands on the factor index, not
plain NIFTY 200. Every expected index has a TR series in data/nifty/TR/.
"""

from __future__ import annotations

from webapp.db import WebDB


def bench(fund_name: str = "", category: str = "Equity",
          index_name: str = "") -> str | None:
    return WebDB._benchmark_index_for(
        {"category": category, "fund_name": fund_name, "index_name": index_name})


def test_index_name_takes_precedence_over_everything():
    assert bench(fund_name="Whatever Fund", index_name="NIFTY_50") == "NIFTY 50"
    assert bench(fund_name="X", index_name="Nifty200_Momentum_30") == \
        "NIFTY200 MOMENTUM 30"
    # even a non-equity category cannot override an explicit tracked index
    assert bench(category="Hybrid", fund_name="Arb", index_name="NIFTY_50") == \
        "NIFTY 50"


def test_nifty_50_family():
    assert bench(fund_name="SBI Nifty 50 ETF") == "NIFTY 50"
    assert bench(fund_name="HDFC NIFTY 50 INDEX FUND") == "NIFTY 50"
    assert bench(fund_name="Nippon India ETF Nifty BeES") == "NIFTY 50"
    assert bench(fund_name="nifty 200 momentum 30 etf") == "NIFTY200 MOMENTUM 30"


def test_specificity_ordering():
    # '500' must win over 'nifty 50' (prefix), momentum-30 over plain 200
    assert bench(fund_name="DSP Nifty 500 Index Fund") == "NIFTY 500"
    assert bench(fund_name="UTI Nifty 200 Momentum 30 ETF") == \
        "NIFTY200 MOMENTUM 30"
    assert bench(fund_name="Kotak Nifty 50 Value 20 ETF") == "NIFTY 50 VALUE 20"
    assert bench(fund_name="ICICI Prudential Nifty Next 50 Index Fund") == \
        "NIFTY NEXT 50"
    assert bench(fund_name="SBI Nifty Next 50 ETF") == "NIFTY NEXT 50"
    assert bench(fund_name="Nippon India ETF Nifty Junior BeES") == \
        "NIFTY NEXT 50"
    assert bench(fund_name="Nifty 500 Equal Weight Fund") == \
        "NIFTY 500 EQUAL WEIGHT"


def test_cap_and_sector_rules():
    assert bench(fund_name="Mirae Asset Nifty Midcap 150 ETF") == \
        "NIFTY MIDCAP 150"
    assert bench(fund_name="Nippon India Nifty Smallcap 250 ETF") == \
        "NIFTY SMALLCAP 250"
    assert bench(fund_name="ICICI Prudential Large Cap Fund") == "NIFTY 100"
    assert bench(fund_name="Nippon India ETF Nifty Bank") == "NIFTY BANK"
    assert bench(fund_name="Nippon India ETF Nifty IT") == "NIFTY IT"
    assert bench(fund_name="Tata Nifty India Consumption ETF") == \
        "NIFTY INDIA CONSUMPTION"


def test_debt_stays_unmapped_and_default_holds():
    assert bench(category="Debt", fund_name="ICICI Pru Short Term Fund") is None
    assert bench(category="Hybrid", fund_name="Equity Savings Fund") is None
    # equity fund matching nothing falls back to the broad-market default
    assert bench(fund_name="Motilal Oswal Nasdaq 100 ETF") == "NIFTY 500"
