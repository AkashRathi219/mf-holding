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
