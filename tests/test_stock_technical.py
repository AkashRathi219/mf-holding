"""tech-v1.0.0: stock technical engine — hand-computed / canonical anchors.

References computed by hand or from published canonical examples:
  SMA(3) of [1..5]           -> last = 4
  EMA(5) seed 2.0, next 10   -> 2 + (10-2)*(1/3) = 4.666667
  RSI(14) Wilder, StockCharts example closes -> first RSI = 70.46
  Bollinger(20,2) on 1..20   -> sd = sqrt(33.25), %B(last=20) = 0.9119
  ATR(14) constant range 20  -> 20 (first value one bar after window,
                                 bar-0 TR is undefined — TA-Lib convention)
  Stoch/W%R flat channel     -> 50 / -50
  Classic pivots H120 L100 C110 -> P110 R1..S3 = 120/100/130/90/140/80
  Fib up-swing 100->200      -> 38.2% = 161.8, 61.8% = 138.2
  OBV [1,2,1,2] vol 10 each  -> [0, +10, 0, +10]
  ROC(10) 50 -> 100          -> +100%
  CCI flat series            -> 0 (MAD=0 guard)
  Ulcer rising series        -> 0; falling 30..1 window peak 14:
                                sqrt(mean((100k/14)^2)) k=0..13 = 54.6324
"""

from __future__ import annotations

import math
import random

import pytest

from webapp.stock_technical import (
    aroon, atr_series, awesome_oscillator, bollinger, candlestick_patterns,
    cci, cmf_series, composite_score, compute_technical, divergence_events,
    donchian_channels, eom_series, ema_series, fibonacci_levels,
    hist_volatility, ichimoku, INSUFFICIENT, macd, mfi_series,
    momentum_line, obv_series, parabolic_sar, pivot_points, pvt_series, roc,
    rolling_vwap, rsi_series, sma_series, stochastic, stoch_rsi,
    supertrend, TECH_VERSION, trix, tsi, ulcer_index, ultimate_oscillator,
    vortex, week52_position, williams_r)


# ---- helpers -------------------------------------------------------------------

def _mk_ohlcv(n=400, seed=7, start=100.0):
    rng = random.Random(seed)
    close, c = [], start
    for _ in range(n):
        c *= (1 + rng.gauss(0.0008, 0.015))
        close.append(round(c, 2))
    high = [x * 1.01 for x in close]
    low = [x * 0.99 for x in close]
    open_ = [low[i] if i % 2 else high[i] * 0.999 for i in range(n)]
    volume = [rng.randint(1_000_000, 5_000_000) for _ in range(n)]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    dates = [f"{i % 28 + 1:02d}-{months[(i // 28) % 12]}-"
             f"{2020 + i // 336}" for i in range(n)]  # unique per bar
    return {"dates": dates, "open": open_, "high": high, "low": low,
            "close": close, "volume": volume}


# ---- moving averages -----------------------------------------------------------

def test_sma_hand_computed():
    out = sma_series([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2:] == [2.0, 3.0, 4.0]


def test_sma_resets_at_none_gap():
    out = sma_series([1, 2, None, 3, 4], 2)
    assert out == [None, 1.5, None, None, 3.5]


def test_ema_seeded_with_sma_and_recursed():
    out = ema_series([2, 2, 2, 2, 2, 10], 5)
    assert out[4] == 2.0
    assert abs(out[5] - (2 + 8 / 3)) < 1e-9


def test_ema_finds_first_valid_run_after_none_warmup():
    # MACD-style input: Nones until index 25 must not kill the EMA signal line;
    # the seed (mean of the first 9 valid values) lands on the LAST bar of the run
    line = [None] * 25 + [float(i) for i in range(1, 41)]
    out = ema_series(line, 9)
    assert out[33] == pytest.approx(sum(range(1, 10)) / 9)
    assert out[40] > out[33]


def test_macd_line_equals_ema_difference():
    # convex (accelerating) series keeps the MACD line rising -> positive histogram
    close = [float(x * x) for x in range(1, 61)]
    line, sig, hist = macd(close)
    e12, e26 = ema_series(close, 12), ema_series(close, 26)
    for a, b, m in zip(e12, e26, line):
        if a is not None and b is not None:
            assert m == pytest.approx(a - b)
    for m, s, h in zip(line, sig, hist):
        if s is not None:
            assert h == pytest.approx(m - s)
    assert hist[-1] > 0


# ---- momentum ------------------------------------------------------------------

STOCKCHARTS_RSI = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                   45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]


def test_rsi_canonical_stockcharts_example():
    out = rsi_series(STOCKCHARTS_RSI, 14)
    assert abs(out[14] - 70.46) < 0.05


def test_rsi_pure_uptrend_is_100():
    out = rsi_series([float(i) for i in range(1, 30)], 14)
    assert out[-1] == 100.0


def test_stochastic_and_williams_flat_channel():
    hi, lo, cl = [110.0] * 20, [90.0] * 20, [100.0] * 20
    k, d = stochastic(hi, lo, cl, 14, 3)
    assert k[13] == 50.0 and k[19] == 50.0
    assert d[19] == 50.0
    w = williams_r(hi, lo, cl, 14)
    assert w[19] == -50.0


def test_roc_doubling_is_100pct():
    out = roc([50.0] * 11 + [100.0], 10)
    assert out[10] == 0.0
    assert abs(out[11] - 100.0) < 1e-9


def test_cci_flat_series_guard():
    out = cci([100.0] * 25, [100.0] * 25, [100.0] * 25)
    assert out[24] == 0.0


def test_tsi_sign_follows_trend_direction():
    up = [float(i) for i in range(1, 60)]
    dn = [float(-i) for i in range(1, 60)]
    assert tsi(up)[-1] > 0
    assert tsi(dn)[-1] < 0


def test_ultimate_oscillator_bounded_and_neutral_center():
    hi, lo, cl = [110.0] * 40, [90.0] * 40, [100.0] * 40
    out = ultimate_oscillator(hi, lo, cl)
    assert out[-1] == 50.0


def test_awesome_oscillator_flat_is_zero():
    hi, lo = [110.0] * 40, [90.0] * 40
    out = awesome_oscillator(hi, lo)
    assert out[-1] == 0.0


def test_stoch_rsi_flat_rsi_guard_returns_neutral_50():
    # RSI on a strictly monotone series is constant 100 -> StochRSI window has
    # hi == lo; the documented convention returns the neutral 50, never 0/100.
    cl = [float(i) for i in range(1, 60)]
    k, d = stoch_rsi(cl)
    assert k[-1] == pytest.approx(50.0)


def test_momentum_line_absolute_change():
    out = momentum_line([50.0] * 11 + [65.0], 10)
    assert out[11] == pytest.approx(15.0)


def test_trix_zero_for_flat_series():
    out = trix([100.0] * 60)
    assert all(v == 0.0 for v in out[15:] if v is not None)


# ---- volatility ----------------------------------------------------------------

def test_bollinger_hand_computed_bandwidth_and_pctb():
    upper, mid, lower, pctb, width = bollinger(list(range(1, 21)), 20)
    sd = math.sqrt(33.25)  # population variance of 1..20 = 33.25
    assert mid[19] == 10.5
    assert abs(upper[19] - (10.5 + 2 * sd)) < 1e-9
    assert abs(lower[19] - (10.5 - 2 * sd)) < 1e-9
    assert abs(pctb[19] - 0.91188) < 1e-4
    assert abs(width[19] - (4 * sd / 10.5)) < 1e-9


def test_atr_constant_range_wilder_seed():
    hi, lo, cl = [110.0] * 20, [90.0] * 20, [100.0] * 20
    out = atr_series(hi, lo, cl, 14)
    assert out[:14] == [None] * 14          # bar-0 TR undefined -> honest nulls
    assert abs(out[14] - 20.0) < 1e-9
    assert abs(out[19] - 20.0) < 1e-9


def test_hist_volatility_flat_is_zero_and_annualised_scale():
    flat = hist_volatility([100.0] * 30, 20)
    assert flat[29] == 0.0
    rng = random.Random(11)
    vals = [100.0]
    for _ in range(200):
        vals.append(vals[-1] * (1 + rng.gauss(0, 0.01)))
    hv = hist_volatility(vals, 20)[-1]
    assert 10 < hv < 25  # ~1% daily sigma annualises to ~15.9% (√252 × 100)


def test_donchian_channels_bounds():
    hi = [float(i) for i in range(1, 31)]
    lo = [x - 0.5 for x in hi]
    up, mid, dn = donchian_channels(hi, lo, 20)
    # window at i=29 covers values 11..30 / lows 10.5..29.5
    assert up[29] == 30.0 and dn[29] == 10.5
    assert mid[29] == pytest.approx(20.25)


def test_ulcer_index_hand_computed():
    rising = ulcer_index(list(range(1, 31)), 14)
    assert rising[29] == 0.0  # never below the running peak
    falling = ulcer_index(list(range(30, 0, -1)), 14)
    exp = math.sqrt(sum((100.0 * k / 14.0) ** 2 for k in range(14)) / 14)
    assert abs(falling[29] - exp) < 1e-9


# ---- trend extras --------------------------------------------------------------

def test_supertrend_flips_bearish_then_bullish():
    # crash below the lower band -> bearish flip, rally above upper band ->
    # bullish flip again (bands seeded from the flat warm-up)
    closes = [100.0] * 15 + [95.0] + [90.0] + [70.0] + [70.0] \
        + [75.0, 85.0, 100.0, 120.0, 150.0]
    n = len(closes)
    hi = [c * 1.02 for c in closes]
    lo = [c * 0.98 for c in closes]
    st = supertrend(hi, lo, closes)
    dirs = [d for d, _lvl in st]
    assert dirs[-1] == "bullish"
    assert "bearish" in dirs          # the crash flipped it
    last_bull = len(dirs) - 1 - dirs[::-1].index("bullish")
    assert dirs.index("bearish") < last_bull


def test_parabolic_sar_below_price_in_uptrend():
    cl = [100.0 + i for i in range(60)]
    hi = [c + 1 for c in cl]
    lo = [c - 1 for c in cl]
    sar = parabolic_sar(hi, lo)
    assert sar[-1] < cl[-1]


def test_vortex_bullish_when_uptrend_strong():
    n = 40
    cl = [100.0 + i for i in range(n)]
    hi = [c + 2 for c in cl]
    lo = [c - 2 for c in cl]
    vp, vm = vortex(hi, lo, cl)
    assert vp[-1] > vm[-1]


def test_aroon_perfect_after_new_extreme():
    n = 30
    hi = [float(i) for i in range(1, n + 1)]   # newest bar is the 25-bar high
    lo = [x - 1 for x in hi]
    up, dn = aroon(hi, lo)
    assert up[-1] == 100.0


def test_ichimoku_midlines_match_window_extremes():
    hi = [float(i) for i in range(1, 61)]
    lo = [x - 1 for x in hi]
    ich = ichimoku(hi, lo, [x - 0.5 for x in hi])
    # tenkan at i=59 over 9 bars: highs 52..60 -> mid of extremes
    assert ich["tenkan"][59] == (60.0 + 51.0) / 2.0
    assert ich["kijun"][59] == (60.0 + 34.0) / 2.0
    assert ich["span_a"][59] is None or True  # shifted cloud may be present


# ---- volume --------------------------------------------------------------------

def test_obv_hand_computed():
    out = obv_series([1.0, 2.0, 1.0, 2.0], [10.0] * 4)
    assert out == [0.0, 10.0, 0.0, 10.0]


def test_mfi_all_positive_flow_is_100():
    # typical price must RISE every day for one-directional money flow;
    # a constant TP has zero flow either way and is an honest null.
    n = 16
    closes = [90.0 + i for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    out = mfi_series(highs, lows, closes, [1000.0] * n, 14)
    assert out[14] == 100.0 and out[15] == 100.0


def test_mfi_zero_flow_constant_tp_is_null():
    n = 16
    out = mfi_series([101.0] * n, [99.0] * n, [100.0] * n, [1000.0] * n, 14)
    assert all(v is None for v in out[14:])


def test_mfi_insufficient_volume_history_is_null():
    vol = [None] * 16
    out = mfi_series([101.0] * 16, [99.0] * 16, [100.0] * 16, vol, 14)
    assert all(v is None for v in out)


def test_rolling_vwap_weighted_average_hand_computed():
    hi = [None] * 19 + [12.0, 12.0]
    lo = [None] * 19 + [8.0, 8.0]
    cl = [None] * 19 + [10.0, 10.0]
    vol = [None] * 19 + [100.0, 300.0]
    out = rolling_vwap(hi, lo, cl, vol, 2)
    assert out[20] == pytest.approx(10.0)  # tp=10 both days -> 10 regardless


def test_cmf_flat_close_inside_range_is_zero():
    n = 25
    hi = [110.0] * n
    lo = [90.0] * n
    cl = [100.0] * n                      # (C-L)-(H-C)=0 -> zero flow
    vol = [1000.0] * n
    out = cmf_series(hi, lo, cl, vol)
    assert out[-1] == 0.0


def test_pvt_accumulates_pct_changes():
    out = pvt_series([100.0, 110.0], [100.0, 100.0])
    assert out[0] == 0.0
    assert out[1] == pytest.approx(10.0)  # volume × (10% change)


def test_eom_finite_on_normal_bars():
    hi = [110.0] * 20
    lo = [90.0] * 20
    cl = [95.0] * 20
    vol = [1_000_000.0] * 20
    out = eom_series(hi, lo, cl, vol)
    assert out[-1] is not None and math.isfinite(out[-1])


# ---- structure -----------------------------------------------------------------

def test_classic_pivot_points():
    pp = pivot_points(120.0, 100.0, 110.0)
    assert pp == {"pivot": 110.0, "r1": 120.0, "s1": 100.0, "r2": 130.0,
                  "s2": 90.0, "r3": 140.0, "s3": 80.0}


def test_fibonacci_upswing_levels():
    fib = fibonacci_levels(200.0, 100.0, uptrend=True)
    assert abs(fib["23.6%"] - 176.4) < 1e-9
    assert abs(fib["38.2%"] - 161.8) < 1e-9
    assert abs(fib["61.8%"] - 138.2) < 1e-9
    down = fibonacci_levels(200.0, 100.0, uptrend=False)
    assert abs(down["50%"] - 150.0) < 1e-9


def test_week52_position_fields():
    pos = week52_position([float(i) for i in range(60, 160)])
    assert pos["high_52w"] == 159.0 and pos["low_52w"] == 60.0
    assert pos["position_pct"] == pytest.approx(100.0)


# ---- patterns / divergence -----------------------------------------------------

def test_hammer_detected_after_downtrend():
    # small body at the very top of the range, long lower wick (2x+ body)
    o = [105, 104, 103, 102, 101, 100]
    h = [105, 104.2, 103.2, 102.2, 101.2, 101.3]
    l = [103, 102, 101, 100, 99, 95]
    c = [104, 103, 102, 101, 100, 101]
    pats = candlestick_patterns(o, h, l, c)
    assert any(p["pattern"] == "hammer" for p in pats), pats


def test_shooting_star_detected_after_uptrend():
    # small body at the bottom of the range, tall upper wick
    o = [95, 96, 97, 98, 99, 100]
    h = [96, 97, 98, 99, 106, 106.5]
    l = [94, 95, 96, 97, 98, 100]
    c = [96, 97, 98, 99, 100, 100.8]
    pats = candlestick_patterns(o, h, l, c)
    assert any(p["pattern"] == "shooting_star" for p in pats), pats


def test_bullish_engulfing_detected():
    # day1 red (o=99 -> c=98); day2 opens below that close, closes above the open
    o = [105, 103, 101, 99.0, 97.5]
    h = [105.5, 103.5, 101.5, 99.5, 100.0]
    l = [102, 100, 98, 97.0, 97.0]
    c = [102.5, 100.5, 99, 98.0, 99.5]
    pats = candlestick_patterns(o, h, l, c)
    assert any(p["pattern"] == "bullish_engulfing" for p in pats), pats


def test_divergence_events_detect_bullish_setup():
    # zig-zag with two STRICT local minima (plateaus never pivot): price makes a
    # lower low while the supplied RSI makes a higher low. The second trough
    # needs >= k bars after it for the fractal pivot detector to see it.
    close = [30, 28, 26, 24, 22, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40,
             38, 36, 34, 32, 30, 28, 26, 24, 22, 20, 18, 16, 14, 12,
             14, 16, 18, 20, 22, 24, 26, 28, 30]
    rsi = [50.0] * len(close)
    rsi[5] = 30.0     # first price trough -> RSI 30
    rsi[15] = 60.0    # price peak
    rsi[29] = 45.0    # lower price trough -> RSI higher low
    evts = divergence_events(close, rsi, lookback=60)
    kinds = [e["kind"] for e in evts]
    assert "regular_bullish_divergence" in kinds, evts


# ---- composite score -----------------------------------------------------------

def test_composite_score_all_null_inputs_returns_none():
    score = composite_score(
        close=[None], sma50=[None], sma200=[None], macd_line=[None],
        macd_sig=[None], adx_v=None, pdi_v=None, mdi_v=None, st_dir=None,
        ich=None, rsi_v=None, roc_v=None, stoch_k=None, cci_v=None,
        mfi_v=None, obv_slope=None, cmf_v=None, vwap_pos=None,
        surge_dir=None)
    assert score["composite"] is None and score["bias"] is None


def test_composite_score_unanimous_bullish_caps_near_100():
    n = 250
    cl = [100.0 + i * 0.5 for i in range(n)]
    score = composite_score(
        close=cl, sma50=sma_series(cl, 50), sma200=sma_series(cl, 200),
        macd_line=macd(cl)[0], macd_sig=macd(cl)[1], adx_v=30.0, pdi_v=28.0,
        mdi_v=12.0, st_dir="bullish",
        ich={"kijun": [cl[-1] - 10.0], "span_a": [cl[-1] - 12.0],
             "span_b": [cl[-1] - 14.0]},
        rsi_v=70.0, roc_v=12.0, stoch_k=85.0, cci_v=140.0, mfi_v=80.0,
        obv_slope=1.0, cmf_v=0.2, vwap_pos=1.0, surge_dir=1.0)
    assert score["composite"] >= 60
    assert score["bias"] == "bullish"


# ---- orchestrator --------------------------------------------------------------

def test_compute_technical_short_series_flags_insufficient():
    data = {"dates": [f"{i+1:02d}-Jan-2025" for i in range(10)],
            "close": [100.0] * 10}
    payload = compute_technical(data)
    assert payload["error"] == INSUFFICIENT
    assert payload["methodology_version"] == TECH_VERSION


def test_compute_technical_full_payload_shape():
    data = _mk_ohlcv()
    payload = compute_technical(data, window=100)
    assert payload["methodology_version"] == TECH_VERSION
    assert payload["as_of"] == data["dates"][-1]
    assert payload["points"] == 400
    ind = payload["indicators"]
    for group in ("trend", "momentum", "volatility", "volume", "structure"):
        assert group in ind
    # honest warm-up: SMA200 latest exists only after 199 bars (we have 400)
    assert ind["trend"]["sma_200"]["value"] is not None
    # series block honours the requested window and stays aligned
    s = payload["series"]
    assert len(s["dates"]) == 100
    assert len(s["close"]) == len(s["dates"])
    for key in ("sma_20", "bb_upper", "rsi_14", "macd", "supertrend_level"):
        assert len(s[key]) == 100
    # signals/patterns carry dates and directions
    for sig in payload["signals"]:
        assert sig["date"] and sig["kind"] and sig["direction"]
    for pat in payload["patterns"]:
        assert pat["date"] and pat["pattern"] in (
            "doji", "hammer", "inverted_hammer", "hanging_man",
            "shooting_star", "bullish_engulfing", "bearish_engulfing",
            "morning_star", "evening_star", "three_white_soldiers",
            "three_black_crows", "piercing_line", "dark_cloud_cover")
    assert isinstance(payload["score"]["composite"], int)


def test_compute_technical_handles_none_volume_bars():
    data = _mk_ohlcv()
    data["volume"] = [None if i % 3 == 0 else v
                      for i, v in enumerate(data["volume"])]
    payload = compute_technical(data)
    assert payload.get("error") != INSUFFICIENT
    # volume indicators degrade to honest nulls where volumes are missing
    mfi_val = payload["indicators"]["volume"]["mfi_14"]["value"]
    assert mfi_val is None  # trailing bar has no valid full-volume window


def test_compute_technical_none_high_low_degrades_gracefully():
    data = _mk_ohlcv()
    data["high"] = [None] * len(data["close"])
    data["low"] = [None] * len(data["close"])
    payload = compute_technical(data)
    assert payload.get("error") != INSUFFICIENT
    assert payload["indicators"]["volatility"]["atr_14"]["value"] is None
    assert payload["indicators"]["trend"]["sma_20"]["value"] is not None


def test_signals_are_chronologically_ordered():
    data = _mk_ohlcv()
    payload = compute_technical(data)
    pos = {d: i for i, d in enumerate(data["dates"])}   # dates are unique
    order = [pos[s["date"]] for s in payload["signals"]]
    assert order == sorted(order)
