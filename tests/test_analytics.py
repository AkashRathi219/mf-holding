"""DBT5: modified-duration metric for debt — unit tests vs hand-computed values.

All expected numbers computed by hand:
  ZC 5y @6%      -> ModDur = 5/1.06            = 4.7170
  1y 8% @par,y8% -> Mac = 1, Mod = 1/1.08      = 0.9259
  2y 10% ytm8%   -> P=103.5666, Mac=1.9106, Mod=1.7691
  same bond priced 100 -> YTM solves to coupon (10%) -> par
"""

from __future__ import annotations

import pytest

from datetime import date, timedelta as td

from webapp.analytics import (cagr_between, compute_series_analytics,
                              daily_returns, max_drawdown)
from webapp.analytics import bullet_modified_duration as dur


def test_zero_coupon_closed_form():
    got = dur(coupon_pct=None, years=5, ytm_pct=6.0)
    assert got["macaulay_duration"] == 5.0
    assert abs(got["modified_duration"] - 4.7170) < 5e-4
    assert got["ytm_pct"] == 6.0


def test_zero_coupon_price_side_t_bill():
    # 182-day T-bill: face 100, discounted price 97 -> annual yield ~6.28%
    got = dur(coupon_pct=0, years=0.5, price=97.0)
    assert got is not None
    assert got["macaulay_duration"] == 0.5
    assert 6.0 < got["ytm_pct"] < 6.5
    assert abs(got["modified_duration"] - 0.5 / (1 + got["ytm_pct"] / 100)) < 1e-3
    assert dur(coupon_pct=0, years=0.5, price=101.0) is None  # impossible quote


def test_one_year_par_band():
    got = dur(coupon_pct=8.0, years=1, ytm_pct=8.0)
    assert got["macaulay_duration"] == 1.0
    assert abs(got["modified_duration"] - 1 / 1.08) < 5e-4


def test_two_year_coupon_bond_hand_computed():
    got = dur(coupon_pct=10.0, years=2, ytm_pct=8.0)
    assert abs(got["macaulay_duration"] - 1.9106) < 5e-4
    assert abs(got["modified_duration"] - 1.7691) < 5e-4


def test_ytm_solved_from_price_lands_on_coupon_for_par():
    got = dur(coupon_pct=10.0, years=2, price=100.0)
    assert abs(got["ytm_pct"] - 10.0) < 1e-6  # par bond yields its coupon
    assert got["macaulay_duration"] > 1.7


def test_semiannual_convention_close_to_annual():
    annual = dur(coupon_pct=9.0, years=10, ytm_pct=9.0)["modified_duration"]
    semi = dur(coupon_pct=9.0, years=10, ytm_pct=9.0,
               coupons_per_year=2)["modified_duration"]
    assert abs(annual - semi) < 0.2  # convention shifts duration slightly only


def test_insufficient_inputs_return_none():
    assert dur(coupon_pct=5.0, years=None, ytm_pct=6.0) is None
    assert dur(coupon_pct=5.0, years=-1, ytm_pct=6.0) is None
    assert dur(coupon_pct=5.0, years=3) is None           # neither ytm nor price
    assert dur(coupon_pct=0.0, years=3) is None           # ZC without any source


def test_db_bond_duration_metrics_wrapper():
    """The webapp wrapper turns maturity_date + coupon/ytm into duration."""
    from datetime import date, timedelta
    from webapp.db import _bond_duration_metrics

    mat = (date.today() + timedelta(days=730)).strftime("%Y-%m-%d")
    got = _bond_duration_metrics(8.0, mat, ytm=8.0)
    assert got is not None
    assert 1.7 < got["macaulay_duration"] < 2.0   # ~2y tenor
    assert abs(got["modified_duration"] - got["macaulay_duration"] / 1.08) < 1e-3

    assert _bond_duration_metrics(8.0, None) is None          # no maturity
    assert _bond_duration_metrics(8.0, "1999-01-01", ytm=8) is None  # matured
    assert _bond_duration_metrics(None, "not-a-date") is None


# ---- ANA1: scheme performance metrics -----------------------------------------


def test_cagr_and_drawdown_hand_computed():
    # DAYS_PER_YEAR=365.25 convention: exponent is 365.25/days
    expected = 1.21 ** (365.25 / 365) - 1
    assert cagr_between(100, 121, 365) == pytest.approx(expected, abs=1e-12)
    assert cagr_between(-1, 2, 100) is None
    assert max_drawdown([100, 110, 99, 105]) == pytest.approx(-0.10)
    assert max_drawdown([1, 2, 3]) == 0.0


def test_daily_returns_skips_bad_points():
    got = daily_returns([100.0, 110.0, 0.0, 55.0])
    assert got == [pytest.approx(0.10)]  # 0-value point breaks the chain


def test_compute_series_monotonic_up():
    """+0.05%/day for 500 days -> CAGR ~20.2%, no drawdown, all-positive 1Y."""
    d0 = date(2024, 6, 1)
    series = [(d0 + td(days=i), 100 * 1.0005 ** i) for i in range(500)]
    out = compute_series_analytics(series)
    assert out["as_of"] == (d0 + td(days=499)).isoformat()
    assert abs(out["cagr_pct"]["since_inception"] - (1.0005 ** 365.25 - 1) * 100) < 0.01
    assert out["risk"]["max_drawdown_pct"] == 0.0
    assert out["rolling_1y"]["pct_positive"] == 100.0
    # near-zero volatility -> Sharpe is honestly None rather than a huge number
    assert out["risk"]["sharpe"] is None or out["risk"]["sharpe"] > 5
    assert out["disclaimer"].startswith("Past performance")


def test_compute_series_benchmark_beta_two():
    """Scheme moves are exactly 2x the benchmark's -> beta 2.0."""
    d0 = date(2024, 6, 1)
    bench, scheme = [], []
    bv = 100.0
    sv = 100.0
    for i in range(400):
        step = (0.001 if i % 2 == 0 else -0.0005)  # alternating up/down
        bv *= (1 + step)
        sv *= (1 + 2 * step)
        bench.append((d0 + td(days=i), bv))
        scheme.append((d0 + td(days=i), sv))
    out = compute_series_analytics(scheme, bench_series=bench)
    b = out["benchmark"]
    assert b is not None and b["n_days"] > 300
    assert b["beta"] == pytest.approx(2.0, abs=1e-2)


def test_compute_series_empty_degrades_honestly():
    out = compute_series_analytics([])
    assert out.get("error")
    short = [("2026-08-01", 100.0), ("2026-08-02", 101.0)]
    out = compute_series_analytics(short)
    assert out["risk"] is None and out["rolling_1y"] is None


def test_short_window_since_inception_is_honest_null():
    """A days-long history must NOT yield an annualized CAGR (compare table
    showed e.g. -17.23% from 5 NAV points before the 90-day floor)."""
    d0 = date(2024, 6, 1)
    week = [(d0 + td(days=i), 100 * (1 + (0.001 if i % 2 else -0.0012)))
            for i in range(6)]
    out = compute_series_analytics(week)
    assert out["cagr_pct"]["since_inception"] is None
    assert out["cagr_pct"]["y1"] is None
    # 90 days exactly -> annualized figure is legitimate again
    span = [(d0 + td(days=i), 100 * 1.0004 ** i) for i in range(0, 91, 30)]
    out = compute_series_analytics(span)
    assert out["cagr_pct"]["since_inception"] is not None


# ---- perf-v1.1: window disclosure + span guards --------------------------------


def test_risk_block_requires_window_span():
    """40 points over 40 days pass the old points-only gate — they must NOT
    produce '3y'-labelled risk stats. The same points over 400 days may, with
    hand-computed annualised volatility."""
    import math

    d0 = date(2024, 6, 1)

    def alternating_series(n: int) -> list:
        """Compounding series whose daily returns alternate -0.1% / +0.2%."""
        out, nav = [], 100.0
        for i in range(n):
            nav *= (1 + (0.002 if i % 2 else -0.001))
            out.append((d0 + td(days=i), nav))
        return out

    out = compute_series_analytics(alternating_series(40))
    assert out["risk"] is None and out["rolling_1y"] is None
    ru = out["risk_unavailable"]
    assert ru["reason"] == "insufficient_history"
    assert ru["required_points"] == 30 and ru["required_span_days"] == 365
    assert ru["found_points"] == 40
    assert ru["found_start"] == d0.isoformat()
    assert ru["found_end"] == (d0 + td(days=39)).isoformat()
    assert ru["window_end"] == (d0 + td(days=39)).isoformat()
    # the considered window itself is disclosed (as_of - round(3*365.25) days)
    assert ru["window_start"] == (date(2024, 7, 10) - td(days=1096)).isoformat()

    out = compute_series_analytics(alternating_series(401))
    r = out["risk"]
    assert r is not None
    assert r["window_start"] == d0.isoformat()
    assert r["window_end"] == (d0 + td(days=400)).isoformat()
    assert r["n_points"] == 401
    # hand-computed vol: 400 daily returns, 200x +0.2% and 200x -0.1%
    m = (200 * 0.002 + 200 * -0.001) / 400
    var = (200 * (0.002 - m) ** 2 + 200 * (-0.001 - m) ** 2) / 399
    expected_vol = math.sqrt(var) * math.sqrt(252) * 100
    assert abs(r["volatility_pct"] - expected_vol) < 0.01
    assert r["max_drawdown_pct"] == pytest.approx(-0.10)


def test_window_dates_emitted_and_complete_flags():
    """Every metric group carries the exact dates it spans; partial windows
    are flagged and their values stay honest nulls."""
    d0 = date(2024, 6, 1)
    n = 1500  # ~4.1 years: 1Y/3Y complete, 5Y partial
    series = [(d0 + td(days=i), 100 * 1.0005 ** i) for i in range(n)]
    out = compute_series_analytics(series)
    as_of = d0 + td(days=n - 1)
    assert out["as_of"] == as_of.isoformat()
    assert out["methodology_version"].startswith("perf-v1.")
    c = out["cagr_pct"]
    # constant daily rate -> every COMPLETE window annualizes to the same CAGR
    expected_cagr = (1.0005 ** 365.25 - 1) * 100
    assert abs(c["since_inception"] - expected_cagr) < 0.01
    assert abs(c["y1"] - expected_cagr) < 0.01
    assert abs(c["y3"] - expected_cagr) < 0.01
    assert c["y1_window"] == {"start": (as_of - td(days=365)).isoformat(),
                              "end": as_of.isoformat(), "days": 365,
                              "points": 366, "complete": True}
    assert c["y3_window"]["complete"] is True
    assert c["y3_window"]["days"] == 1096
    # 5Y requested, ~4.1y available: dates shown, value honestly null
    assert c["y5"] is None
    assert c["y5_window"]["complete"] is False
    assert c["y5_window"]["start"] == d0.isoformat()
    assert c["since_inception_window"] == {
        "start": d0.isoformat(), "end": as_of.isoformat(),
        "days": n - 1, "points": n, "complete": True}
    # risk slice = points inside as_of - round(3*365.25) = as_of - 1096
    r = out["risk"]
    assert r["window_start"] == (as_of - td(days=1096)).isoformat()
    assert r["window_end"] == as_of.isoformat()
    assert r["n_points"] == n - (n - 1 - 1096)
    assert r["max_drawdown_pct"] == 0.0  # monotonic rise
    assert r["sharpe"] is None or r["sharpe"] > 5  # ~zero vol: honest null
    roll = out["rolling_1y"]
    assert roll["window_days"] == 365
    assert roll["first_window_start"] == d0.isoformat()
    assert roll["last_window_end"] == as_of.isoformat()
    assert roll["n_periods"] == n - 360  # ends at indices 360..1499 (360d min per the -5d tolerance)
    assert roll["pct_positive"] == 100.0


def test_benchmark_block_span_guard_and_window_dates():
    """Benchmark stats need >=365 days of overlap and disclose the common span."""
    d0 = date(2024, 6, 1)
    bench, scheme = [], []
    bv = sv = 100.0
    for i in range(400):  # 399 common days < 365? no: 399 >= 30 but span 398 < 365? -> 398 days
        step = (0.001 if i % 2 == 0 else -0.0005)
        bv *= (1 + step)
        sv *= (1 + 2 * step)
        bench.append((d0 + td(days=i), bv))
        scheme.append((d0 + td(days=i), sv))
    # 400 daily points -> common return dates span 398 days < 365? 398 > 365 -> computed
    out = compute_series_analytics(scheme, bench_series=bench)
    b = out["benchmark"]
    assert b is not None
    assert b["window_start"] == (d0 + td(days=1)).isoformat()
    assert b["window_end"] == (d0 + td(days=399)).isoformat()
    assert (date.fromisoformat(b["window_end"])
            - date.fromisoformat(b["window_start"])).days >= 365
    assert b["beta"] == pytest.approx(2.0, abs=1e-2)

    # a short overlap (60 days) must yield an honest null, not 5-week beta
    bench_s = bench[:61]
    scheme_s = scheme[:61]
    out = compute_series_analytics(scheme_s, bench_series=bench_s)
    assert out["benchmark"] is None
