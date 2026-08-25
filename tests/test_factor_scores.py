"""factor-v1.0.0: cross-sectional factor engine — hand-computed anchors.

References computed by hand:
  _window_return: closes ending [.., 100, 200], lookback 5 -> 200/150 - 1
  OLS beta of y = 1.5x            -> 1.5 exactly
  percentile rank of the median   -> 50.0 (odd-sized cross-section)
  inverted ranking (low vol best) -> lowest raw value ranks 100
  composite gate                  -> needs >= MIN_COMPONENTS (3) components
"""

from __future__ import annotations

import math

import pytest

from webapp.factor_scores import (
    FACTOR_VERSION, MOMENTUM_COMPONENTS, MIN_COMPONENTS, LOWVOL_COMPONENTS,
    _ols_beta, _percentile_rank, _window_return, _winsorized_z,
    composite_from_ranks, compute_factor_scores, lowvol_metrics,
    momentum_metrics, rank_component, screen, value_metrics)


# ---- per-stock metrics ---------------------------------------------------------

def test_window_return_hand_computed():
    closes = [100.0] * 10 + [120.0, 140.0, 160.0, 180.0, 200.0]
    # lookback 5 ending today: start = index len-1-5 = 9 -> base 100
    assert _window_return(closes, 5) == pytest.approx(200 / 100 - 1)
    # skip 2: ends at index 12 (160), start index 7 (still the 100-block)
    assert _window_return(closes, 5, skip=2) == pytest.approx(160 / 100 - 1)


def test_momentum_metrics_skip_month_layout():
    # near-geometric series with deterministic wiggle so sigma is non-zero;
    # expected values recomputed independently from the same definition.
    n = 400
    rets = [0.01 + 0.002 * math.sin(i) for i in range(n - 1)]
    closes = [100.0]
    for r in rets:
        closes.append(closes[-1] * (1 + r))
    m = momentum_metrics(closes)
    end_i = n - 1 - 21
    start_i = end_i - (252 - 21)
    exp_12_1 = closes[end_i] / closes[start_i] - 1
    assert m["ret_12_1"] == pytest.approx(exp_12_1, rel=1e-9)
    tail = closes[-252:]
    tr = [b / a - 1 for a, b in zip(tail, tail[1:])]
    mtr = sum(tr) / len(tr)
    sd = math.sqrt(sum((r - mtr) ** 2 for r in tr) / (len(tr) - 1))
    assert m["ret_12_1_risk_adj"] == pytest.approx(
        exp_12_1 / (sd * math.sqrt(252)), rel=1e-6)


def test_momentum_metrics_short_history_all_null():
    out = momentum_metrics([100.0] * 50)
    assert all(v is None for v in out.values())
    assert set(out.keys()) == set(MOMENTUM_COMPONENTS)


def test_lowvol_flat_series_zero_risk_no_drawdown():
    out = lowvol_metrics([100.0] * 300)
    assert out["vol_252"] == 0.0
    assert out["max_drawdown"] == 0.0
    assert out["downside_dev"] is None      # no negative returns at all
    assert out["beta"] is None              # no benchmark supplied


def test_lowvol_drawdown_hand_computed():
    closes = [100.0] * 200 + [90.0] + [95.0]
    out = lowvol_metrics(closes)
    assert out["max_drawdown"] == pytest.approx(-0.10)


def test_lowvol_insufficient_bars_null():
    out = lowvol_metrics([100.0] * 50)
    assert all(v is None for v in out.values())
    assert set(out.keys()) == set(LOWVOL_COMPONENTS)


def test_ols_beta_exact_linear_relation():
    xs = [0.001 * i for i in range(1, 120)]
    ys = [1.5 * x for x in xs]
    assert _ols_beta(ys, xs) == pytest.approx(1.5, rel=1e-9)


def test_value_metrics_yields_and_inversion():
    snap = {"eps": 10.0, "bvps": 500.0, "sps": 800.0, "cfps": 20.0,
            "ebitda_total": 30.0, "net_debt_total": 70.0, "dps_ttm": 5.0}
    v = value_metrics(price=250.0, fundamentals=snap)
    assert v["earnings_yield"] == pytest.approx(0.04)
    assert v["book_yield"] == pytest.approx(2.0)
    assert v["sales_yield"] == pytest.approx(3.2)
    assert v["cashflow_yield"] == pytest.approx(0.08)
    assert v["dividend_yield"] == pytest.approx(0.02)
    # EV per share = price + net-debt per share = 320; EBITDA/share 30
    assert v["ev_ebitda_inv"] == pytest.approx(30.0 / 320.0)


def test_value_metrics_without_financials_is_null_not_zero():
    v = value_metrics(price=100.0, fundamentals=None)
    assert all(v[k] is None for k in v)
    v2 = value_metrics(price=None, fundamentals={"eps": 10.0})
    assert all(v2[k] is None for k in v2)


# ---- cross-section -------------------------------------------------------------

def test_winsorized_z_clips_outlier():
    values = [10.0] * 19 + [10_000.0]
    zs = _winsorized_z(values)
    assert max(zs) <= 3.0 + 1e-9


def test_percentile_rank_median_is_fifty():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    zs = _winsorized_z(vals)
    zmap = dict(zip(vals, zs))
    assert _percentile_rank(zmap[3.0], zs) == pytest.approx(50.0)


def test_rank_component_inverts_low_vol_so_lower_is_stronger():
    vols = {"a": 5.0, "b": 10.0, "c": 15.0, "d": 20.0, "e": 25.0, "f": 30.0}
    ranks, n = rank_component(vols, component="vol_252")
    assert n == 6
    assert ranks["a"] == 100.0        # lowest volatility = strongest low-vol
    assert ranks["a"] > ranks["c"] > ranks["f"]
    # non-inverted component keeps the natural direction
    rets = {"a": 5.0, "b": 10.0, "c": 15.0, "d": 20.0, "e": 25.0, "f": 30.0}
    r2, _ = rank_component(rets, component="ret_12_1")
    assert r2["f"] == 100.0 and r2["f"] > r2["c"] > r2["a"]


def test_rank_component_thin_cross_section_returns_empty():
    ranks, n = rank_component({"a": 1.0, "b": 2.0, "c": 3.0},
                              component="ret_12_1")
    assert ranks == {} and n == 3


def test_rank_component_skips_nulls():
    raw = {k: float(i * 10) for i, k in enumerate("abcdefgh")}
    raw["z"] = None
    raw["y"] = float("nan")
    ranks, n = rank_component(raw, component="ret_12_1")
    assert "z" not in ranks and "y" not in ranks and n == 8


def test_composite_requires_min_components():
    one = {"c0": {"a": 40.0, "b": 60.0}}
    got = composite_from_ranks(one)
    assert got == {}                      # single component -> excluded
    enough = {f"c{i}": {"a": 40.0 + i, "b": 60.0, "c": 55.0}
              for i in range(MIN_COMPONENTS)}
    got = composite_from_ranks(enough)
    assert got["a"] == pytest.approx(sum(40.0 + i
                                         for i in range(MIN_COMPONENTS))
                                     / MIN_COMPONENTS)


def _synthetic_universe(n=12):
    """n stocks with rising momentum and falling volatility across isins."""
    raw = {}
    for i in range(n):
        tag = f"ISIN{i:03d}"
        raw[tag] = {
            "momentum": {"ret_12_1": -0.2 + 0.05 * i,
                         "ret_6_1": -0.1 + 0.03 * i,
                         "ret_3_1": -0.05 + 0.02 * i,
                         "ret_12_1_risk_adj": -1.0 + 0.3 * i},
            "lowvol": {"vol_252": 40.0 - 2.0 * i,          # falling vol
                       "downside_dev": 30.0 - 1.5 * i,
                       "beta": 2.0 - 0.15 * i,
                       "max_drawdown": -0.05 - 0.01 * i},   # deeper dd worse
        }
    return raw


def test_compute_factor_scores_end_to_end():
    raw = _synthetic_universe()
    sectors = {isin: ("A" if int(isin[-3:]) % 2 == 0 else "B")
               for isin in raw}
    payload = compute_factor_scores(raw, sectors)
    assert payload["methodology_version"] == FACTOR_VERSION
    assert payload["universe_n"] == len(raw)
    fs = payload["factor_scores"]
    # strongest momentum stock should outrank the weakest
    top = fs[f"ISIN{11:03d}"]["momentum"]
    bot = fs["ISIN000"]["momentum"]
    assert top > bot
    # low-vol factor: ISIN011 has the LOWEST vol -> highest score
    assert fs["ISIN011"]["lowvol"] > fs["ISIN000"]["lowvol"]
    # multi-factor exists and orders sensibly
    assert payload["multi_factor"][f"ISIN{11:03d}"] > \
        payload["multi_factor"]["ISIN000"]
    # sector-relative produced within-bucket ranks for both groups
    assert payload["sector_relative"]
    # value/quality honestly null everywhere (no financials supplied)
    assert not any("value" in s or "quality" in s for s in fs.values())


def test_screen_orders_and_clamps():
    raw = _synthetic_universe(14)
    payload = compute_factor_scores(raw)
    rows = screen(payload, factor="momentum", top_n=5)
    assert len(rows) == 5
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    asc = screen(payload, factor="momentum", top_n=3, ascending=True)
    assert asc[0]["score"] <= asc[-1]["score"]
    huge = screen(payload, factor="multi", top_n=99_999)
    assert len(huge) <= len(payload["multi_factor"])
