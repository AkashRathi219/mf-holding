"""perf-v2.0.0 regressions — each test pins one audit finding.

Findings under test (financial-metrics audit, Aug 2026):
F1  Information ratio was divided by DAILY tracking error (~15.9x inflation).
F2  Funds younger than 3y got fabricated NEGATIVE Sharpe/Sortino
    (`cagr3 or 0.0` fallback against the rf penalty).
F4  Malformed NAV-history files crashed the market-value index rebuild.
F6  Rolling-1Y chart and KPI card used different windows/tolerances.
F8  IDCW payouts were invisible to TWR/XIRR; reinvestments created phantom
    opening units.
F9  Mixed pricing basis inflated reweighted portfolio totals past 100%.
"""

from __future__ import annotations

import math
from datetime import date, timedelta as td

import pytest

from webapp.analytics import (DAYS_PER_YEAR, TRADING_DAYS,
                              benchmark_relative_stats, bullet_modified_duration,
                              classify_tx_type,
                              compute_series_analytics, rolling_returns)


# ---- F1: information ratio must use ANNUALISED tracking error -------------------

def _aligned_series(n_days: int, scheme_step, bench_step):
    d0 = date(2024, 1, 1)
    bv = sv = 100.0
    s_map, b_map = {}, {}
    for i in range(n_days):
        bs = bench_step(i)
        ss = scheme_step(i)
        bv *= (1 + bs)
        sv *= (1 + ss)
        key = (d0 + td(days=i + 1)).isoformat()
        b_map[key] = bs
        s_map[key] = ss
    return s_map, b_map


def test_information_ratio_is_annualised_not_inflated():
    # noisy benchmark; scheme = benchmark + alternating active return
    def bench_step(i):
        return 0.001 if i % 2 == 0 else -0.0005

    def sch(i):
        return bench_step(i) + (0.0003 if i % 2 else -0.0001)  # mean +1bp
    s2, b2 = _aligned_series(420, sch, bench_step)
    out = benchmark_relative_stats(s2, b2)
    assert out is not None
    act = [(s2[k] - b2[k]) for k in sorted(s2)]
    m = sum(act) / len(act)
    sd = math.sqrt(sum((x - m) ** 2 for x in act) / (len(act) - 1))
    expected_ir = (m * TRADING_DAYS) / (sd * math.sqrt(TRADING_DAYS))
    assert out["information_ratio"] == pytest.approx(expected_ir, abs=0.01)
    # regression pin: the OLD bug reported ~15.9x this number
    assert out["information_ratio"] < expected_ir * 1.5
    assert out["tracking_error_pct"] == pytest.approx(sd * math.sqrt(TRADING_DAYS) * 100, abs=0.005)
    assert "r_squared" in out and "method" in out


def test_benchmark_beta_two_still_works():
    d0 = date(2024, 1, 1)
    bench, scheme = [], []
    bv = sv = 100.0
    for i in range(400):
        step = (0.001 if i % 2 == 0 else -0.0005)
        bv *= (1 + step)
        sv *= (1 + 2 * step)
        bench.append((d0 + td(days=i), bv))
        scheme.append((d0 + td(days=i), sv))
    out = compute_series_analytics(scheme, bench_series=bench)
    assert out["benchmark"]["beta"] == pytest.approx(2.0, abs=1e-2)


# ---- F2: window-paired Sharpe/Sortino for young funds ---------------------------

def test_young_fund_gets_positive_window_paired_sharpe():
    """A 1.1-year-old fund compounding at +17% must NOT show Sharpe -0.4."""
    d0 = date(2025, 7, 1)
    series = [(d0 + td(days=i), 100 * 1.0005 ** i) for i in range(401)]
    out = compute_series_analytics(series)
    r = out["risk"]
    assert r is not None                      # span 400d >= 365d gate
    assert r["window_years"] == pytest.approx(400 / DAYS_PER_YEAR, abs=0.02)
    assert 0.9 < r["window_years"] < 1.2      # honestly labelled ~1.1y
    # numerator pairs the SAME window as the volatility denominator
    span = 400
    v0, v1 = 100.0, 100 * 1.0005 ** span
    cagr_win = (v1 / v0) ** (DAYS_PER_YEAR / span) - 1
    assert r["return_cagr_pct"] == pytest.approx(cagr_win * 100, abs=0.05)
    assert r["sharpe"] is None or r["sharpe"] > 0   # NEVER negative here


def test_old_negative_sharpe_fabrication_is_gone():
    """Direct regression for the audit finding: HDFC-Innovation-like profile
    (+17.3% SI CAGR, 14% vol, 1.1y history) previously scored Sharpe -0.425."""
    import random
    rng = random.Random(42)
    d0 = date(2025, 7, 1)
    nav = 100.0
    series = []
    for i in range(401):                       # ~1.1y of daily prints
        if i:
            nav *= (1 + 0.00045 + rng.gauss(0, 0.0089))  # ~17%/yr, ~14% vol
        series.append((d0 + td(days=i), nav))
    out = compute_series_analytics(series)
    si_cagr = out["cagr_pct"]["since_inception"]
    r = out["risk"]
    assert r is not None and si_cagr is not None and si_cagr > 10
    assert r["sharpe"] is not None
    # paired-window identity: sharpe == (return_cagr - rf)/vol
    implied = (r["return_cagr_pct"] / 100 - 0.06) / (r["volatility_pct"] / 100)
    assert r["sharpe"] == pytest.approx(implied, abs=0.02)


# ---- F5: zero-coupon yield cap removed (honest null, no silent clamp) ----------

def test_zc_implausible_yield_is_null_not_capped():
    # price 30 on 1y ZC implies ~233% yield: old code silently capped at 200%
    assert bullet_modified_duration(coupon_pct=0, years=1, price=30.0) is None
    got = bullet_modified_duration(coupon_pct=0, years=1, price=97.0)
    assert got is not None
    assert got["ytm_effective_annual_pct"] == pytest.approx(got["ytm_pct"])


# ---- F6: ONE rolling core feeds KPI cards AND charts ----------------------------

def test_chart_points_are_a_strided_subset_of_the_kpi_core():
    from webapp.db import WebDB
    d0 = date(2024, 1, 1)
    n = 800
    dates, vals = [], []
    v = 100.0
    for i in range(n):
        if i:
            v *= (1 + (0.0011 if i % 3 else -0.0006))
        dates.append((d0 + td(days=i)).isoformat())
        vals.append(round(v, 4))
    parsed = [(date.fromisoformat(d), v) for d, v in zip(dates, vals)]
    core = {(d.isoformat(), round(r * 100, 2))
            for d, r, _b in rolling_returns(parsed)}
    chart = WebDB._rolling_1y_points(dates, vals)
    assert chart
    members = {(d, v) for d, v in chart}
    assert members <= core                     # every chart point IS a KPI row
    spacing = [(date.fromisoformat(chart[i + 1][0])
                - date.fromisoformat(chart[i][0])).days
               for i in range(len(chart) - 1)]
    assert min(spacing) >= 7                   # calendar-day stride, not rows


# ---- F8: IDCW payouts & reinvestments in movement analytics ----------------------

def _nav_map(start: date, days: int, rate: float, first: float = 10.0) -> dict:
    out = {}
    v = first
    for i in range(days):
        if i:
            v = round(v * (1 + rate), 6)
        out[(start + td(days=i)).isoformat()] = v
    return out


def _lookup(nav: dict):
    return lambda amfi_code, isin: (nav, "test")


def test_idcw_payout_is_cash_flow_not_invisible():
    """Ex-date NAV -10% with an exactly offsetting payout => TWR 0%, and the
    payout appears in cash_out instead of vanishing."""
    from webapp.analytics import portfolio_movement_series
    d0 = date(2026, 1, 1)
    nav = {d0.isoformat(): 10.0,
           (d0 + td(days=1)).isoformat(): 9.0,     # -10% ex-payout
           (d0 + td(days=2)).isoformat(): 9.0}
    items = [{"isin": "INF000000101", "name": "T", "units": 100.0,
              "amfi_code": "700001"}]
    tx = [{"date": d0.isoformat(), "type": "PURCHASE", "flow_kind": "cash_in",
           "sign": 1.0, "units": 100.0, "amount": 1000.0, "cum_units": 100.0,
           "isin": "INF000000101", "amfi_code": "700001", "name": "T",
           "nav": 10.0},
          {"date": (d0 + td(days=1)).isoformat(), "type": "IDCW",
           "flow_kind": "income", "sign": -1.0, "units": 0.0,
           "amount": -100.0, "cum_units": 0.0,
           "isin": "INF000000101", "amfi_code": "700001", "name": "T",
           "nav": 9.0}]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    assert got["data_note"]["income_tx_count"] == 1
    # signed sums: the payout LEFT the portfolio (cash_out is negative-signed)
    assert got["cash_out"] == pytest.approx(-100.0)
    assert got["cash_in"] == pytest.approx(1000.0)
    assert got["total_twr_pct"] == pytest.approx(0.0, abs=0.01)


def test_reinvestment_creates_no_phantom_opening_units():
    from webapp.analytics import portfolio_movement_series
    d0 = date(2026, 1, 1)
    nav = _nav_map(d0, 10, 0.0)                        # flat NAV 10.0
    items = [{"isin": "INF000000101", "name": "T", "units": 105.0,
              "amfi_code": "700001"}]
    tx = [{"date": d0.isoformat(), "type": "PURCHASE", "flow_kind": "cash_in",
           "sign": 1.0, "units": 100.0, "amount": 1000.0, "cum_units": 100.0,
           "isin": "INF000000101", "amfi_code": "700001", "name": "T",
           "nav": 10.0},
          {"date": (d0 + td(days=5)).isoformat(),
           "type": "IDCW REINVESTMENT", "flow_kind": "reinvest", "sign": 1.0,
           "units": 5.0, "amount": 0.0, "cum_units": 5.0,
           "isin": "INF000000101", "amfi_code": "700001", "name": "T",
           "nav": 10.0}]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    c = got["constituents"][0]
    assert c["opening_units"] == pytest.approx(0.0, abs=1e-6)   # fully explained
    assert got["data_note"]["reinvest_tx_count"] == 1
    assert got["total_net_flow"] == pytest.approx(1000.0)       # reinvest not a flow


def test_classify_tx_type_vocabulary():
    assert classify_tx_type("PURCHASE") == "cash_in"
    assert classify_tx_type("SYSTEMATIC INVESTMENT") == "cash_in"
    assert classify_tx_type("redeem") == "cash_out"
    assert classify_tx_type("SWITCH_IN") == "internal"
    assert classify_tx_type("SWITCH-OUT") == "internal"
    assert classify_tx_type("IDCW") == "income"
    assert classify_tx_type("DIVIDEND PAYOUT") == "income"
    assert classify_tx_type("IDCW_REINVESTMENT") == "reinvest"
    assert classify_tx_type("DIVIDEND REINVESTMENT") == "reinvest"
    assert classify_tx_type("BONUS") == "reinvest"
    assert classify_tx_type("MYSTERY") == "unknown"
    assert classify_tx_type("") == "unknown"


# ---- F9: all-or-nothing pricing basis -------------------------------------------

def test_unpriced_line_keeps_whole_portfolio_on_cost_basis(monkeypatch):
    from webapp import market_value as mv

    class _FakeDB:
        def _resolve_scheme_item(self, item):
            return None

    monkeypatch.setattr(mv, "latest_nav_index",
                        lambda force=False: {"INE000GOOD11": {"nav": 100.0,
                                                              "date": "2026-08-20"}})
    import webapp.db as dbmod
    monkeypatch.setattr(dbmod, "WebDB", _FakeDB)

    items = [
        {"type": "stock", "isin": "INE000GOOD11", "name": "Priced Stock",
         "units": 10, "weight": 50.0},
        {"type": "stock", "isin": "INE000MISS22", "name": "Unpriced Stock",
         "units": 5, "weight": 50.0},
    ]
    out = mv.reweight_by_market_value(items)
    bases = {it.get("pricing_basis") for it in out}
    assert bases == {"cost"}                    # NO mixed basis, ever
    weights = sorted(it["weight"] for it in out)
    assert weights == [50.0, 50.0]              # untouched originals


def test_normalize_metric_guardrail():
    from webapp.conventions import normalize_metric
    assert normalize_metric(0.0072, "ter") == 0.0072          # clean fraction
    assert normalize_metric(1.09, "ter") == 0.0109            # percent-scale fixed
    assert normalize_metric(29.61, "ytm") == 0.2961           # /100 lands in band
    assert normalize_metric(29.61, "ter") == 29.61            # hopeless either way:
    #   -> passthrough for data-health review (never silently mangled)
    assert normalize_metric(None, "ter") is None
    assert normalize_metric("x", "ter") is None
    assert normalize_metric(-0.5, "ter") == -0.5              # hopeless -> caller rejects
