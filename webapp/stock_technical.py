"""Technical-analysis engine for stocks [tech-v1.0.0].

Pure functions, no I/O — every indicator here is unit-tested against
hand-computed or canonical values (tests/test_stock_technical.py).
Mirrors the house rules of webapp/analytics.py:

- Honest nulls: an indicator that needs n periods emits None until its
  window is full; a series shorter than the period yields all-None.
  Never zero, never a partial-window value.
- As-of = last bar in the supplied series, never wall-clock today.
- Inputs may carry None (Yahoo-era close-only bars, missing volume):
  any window containing None produces None at those positions.

Entry point: compute_technical(data) over {dates, open, high, low,
close, volume} aligned lists -> latest value + signal per indicator,
recent crossover/divergence events, candlestick patterns, chart-ready
overlay series (trailing ``window`` points) and a composite score with
per-component contributions.

Compliance: descriptive computations over public NSE/Yahoo price data.
Signals are arithmetic readings of past prices — callers must render the
standard "not investment advice" disclaimer beside them.
"""
from __future__ import annotations

import math
from collections import deque

from .conventions import TRADING_DAYS

TECH_VERSION = "tech-v1.0.0"

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"
INSUFFICIENT = "insufficient_data"


# ---- generic helpers -----------------------------------------------------------

def _sma(values: list[float | None], n: int) -> list[float | None]:
    """Simple moving average; resets at None gaps (no carry across holes)."""
    out: list[float | None] = [None] * len(values)
    if n <= 0:
        return out
    q: deque[float] = deque()
    s = 0.0
    for i, v in enumerate(values):
        if v is None:
            q.clear()
            s = 0.0
            continue
        q.append(v)
        s += v
        if len(q) > n:
            s -= q.popleft()
        if len(q) == n:
            out[i] = s / n
    return out


def _ema(values: list[float | None], n: int) -> list[float | None]:
    """EMA seeded with the mean of the FIRST run of n consecutive valid
    values, then recursed with alpha = 2/(n+1); None gaps pause recursion."""
    out: list[float | None] = [None] * len(values)
    if n <= 0:
        return out
    k = 2.0 / (n + 1.0)
    run: list[int] = []
    seed_at: int | None = None
    for i, v in enumerate(values):
        if v is None:
            run.clear()
            continue
        run.append(i)
        if len(run) == n:
            seed_at = i
            break
    if seed_at is None:
        return out
    prev = sum(values[j] for j in run) / n
    out[seed_at] = prev
    for i in range(seed_at + 1, len(values)):
        v = values[i]
        if v is None:
            continue
        prev = prev + k * (v - prev)
        out[i] = prev
    return out


def _wilder(values: list[float | None], n: int) -> list[float | None]:
    """Wilder smoothing seeded with the mean of the first n valid values."""
    out: list[float | None] = [None] * len(values)
    vals = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(vals) < n:
        return out
    prev = sum(v for _, v in vals[:n]) / n
    out[vals[n - 1][0]] = prev
    for idx, v in vals[n:]:
        prev = prev - prev / n + v / n
        out[idx] = prev
    return out


def _windowed(values: list[float | None], n: int, fn) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i < n - 1:
            continue
        w = values[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        out[i] = fn(w)
    return out


def _std_pop(w: list[float]) -> float:
    m = sum(w) / len(w)
    return math.sqrt(sum((v - m) ** 2 for v in w) / len(w))


def _true_range(high: list[float | None], low: list[float | None],
                close: list[float | None]) -> list[float | None]:
    tr: list[float | None] = []
    for i in range(len(close)):
        h, l, c = high[i], low[i], close[i]
        pc = close[i - 1] if i > 0 else None
        if h is None or l is None or c is None or pc is None:
            tr.append(None)
            continue
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def _cross_events(fast: list[float | None], slow: list[float | None]
                  ) -> list[tuple[int, str]]:
    """Indices where sign(fast-slow) flips -> [(i, 'up'|'down'), ...]."""
    evts: list[tuple[int, str]] = []
    prev_diff: float | None = None
    for i, (f, s) in enumerate(zip(fast, slow)):
        if f is None or s is None:
            prev_diff = None
            continue
        d = f - s
        if prev_diff is not None:
            if prev_diff <= 0 < d:
                evts.append((i, "up"))
            elif prev_diff >= 0 > d:
                evts.append((i, "down"))
        prev_diff = d
    return evts


# ---- moving averages / trend ---------------------------------------------------

def sma_series(close: list[float | None], n: int) -> list[float | None]:
    return _sma(close, n)


def ema_series(close: list[float | None], n: int) -> list[float | None]:
    return _ema(close, n)


def macd(close: list[float | None], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[list, list, list]:
    """MACD line (EMA12-EMA26), signal (EMA9 of line), histogram."""
    line = [a - b if a is not None and b is not None else None
            for a, b in zip(_ema(close, fast), _ema(close, slow))]
    sig = _ema(line, signal)
    hist = [a - b if a is not None and b is not None else None
            for a, b in zip(line, sig)]
    return line, sig, hist


def adx(high: list[float | None], low: list[float | None],
        close: list[float | None], n: int = 14) -> tuple[list, list, list]:
    size = len(close)
    plus_dm: list[float | None] = [None] * size
    minus_dm: list[float | None] = [None] * size
    for i in range(1, size):
        h, l, ph, pl = high[i], low[i], high[i - 1], low[i - 1]
        if None in (h, l, ph, pl):
            continue
        up, dn = h - ph, pl - l
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
    tr = _true_range(high, low, close)
    str_ = _wilder(tr, n)
    spdm, smdm = _wilder(plus_dm, n), _wilder(minus_dm, n)
    pdi = [100 * p / t if p is not None and t else None
           for p, t in zip(spdm, str_)]
    mdi = [100 * m / t if m is not None and t else None
           for m, t in zip(smdm, str_)]
    dx = [abs(p - m) / (p + m) * 100
          if p is not None and m is not None and (p + m) else None
          for p, m in zip(pdi, mdi)]
    adx_line = _wilder(dx, n)
    return adx_line, pdi, mdi


def supertrend(high: list[float | None], low: list[float | None],
               close: list[float | None], period: int = 10,
               mult: float = 3.0) -> list[tuple[str, float | None]]:
    """[(direction, level)]; ATR(10) x 3 bands, flip on close-through."""
    atr = _wilder(_true_range(high, low, close), period)
    out: list[tuple[str, float | None]] = [("neutral", None)] * len(close)
    upper_b = lower_b = None
    direction, st_level = "bullish", None
    for i in range(len(close)):
        h, l, c, a = high[i], low[i], close[i], atr[i]
        if None in (h, l, c, a):
            continue
        mid = (h + l) / 2.0
        bu, bl = mid + mult * a, mid - mult * a
        pc = close[i - 1] if i else None
        if upper_b is None:
            upper_b, lower_b, direction = bu, bl, "bullish"
            st_level = bl
            out[i] = (direction, st_level)
            continue
        if bu < upper_b or (pc is not None and pc > upper_b):
            upper_b = bu
        if bl > lower_b or (pc is not None and pc < lower_b):
            lower_b = bl
        if direction == "bullish":
            if c < lower_b:
                direction, st_level = "bearish", upper_b
            else:
                st_level = lower_b
        else:
            if c > upper_b:
                direction, st_level = "bullish", lower_b
            else:
                st_level = upper_b
        out[i] = (direction, st_level)
    return out


def parabolic_sar(high: list[float | None], low: list[float | None],
                  af_step: float = 0.02, af_max: float = 0.2
                  ) -> list[float | None]:
    size = len(low)
    out: list[float | None] = [None] * size
    trend_up, sar, ep, af, started = True, None, None, af_step, False
    for i in range(size):
        h, l = high[i], low[i]
        if h is None or l is None:
            continue
        ph1 = high[i - 1] if i >= 1 and high[i - 1] is not None else None
        pl1 = low[i - 1] if i >= 1 and low[i - 1] is not None else None
        ph2 = high[i - 2] if i >= 2 and high[i - 2] is not None else None
        pl2 = low[i - 2] if i >= 2 and low[i - 2] is not None else None
        if not started:
            sar, ep, started = l, h, True
            out[i] = sar
            continue
        sar = sar + af * (ep - sar)
        if trend_up:
            guards = [g for g in (pl1, pl2) if g is not None]
            if guards:
                sar = min(sar, min(guards))
            if h > ep:
                ep, af = h, min(af + af_step, af_max)
            if l < sar:
                trend_up, sar, ep, af = False, ep, l, af_step
        else:
            guards = [g for g in (ph1, ph2) if g is not None]
            if guards:
                sar = max(sar, max(guards))
            if l < ep:
                ep, af = l, min(af + af_step, af_max)
            if h > sar:
                trend_up, sar, ep, af = True, ep, h, af_step
        out[i] = sar
    return out


def aroon(high: list[float | None], low: list[float | None],
          n: int = 25) -> tuple[list, list]:
    def since_extreme(vals: list[float | None], i: int,
                      is_high: bool) -> int | None:
        if i < n:
            return None
        w = vals[i - n:i + 1]
        if any(v is None for v in w):
            return None
        target = max(w) if is_high else min(w)
        for back, v in enumerate(reversed(w)):
            if v == target:
                return back
        return None

    up: list[float | None] = [None] * len(high)
    down: list[float | None] = [None] * len(high)
    for i in range(len(high)):
        sh = since_extreme(high, i, True)
        sl = since_extreme(low, i, False)
        if sh is not None:
            up[i] = 100.0 * (n - sh) / n
        if sl is not None:
            down[i] = 100.0 * (n - sl) / n
    return up, down


def ichimoku(high: list[float | None], low: list[float | None],
             close: list[float | None], conv: int = 9, base: int = 26,
             spanb: int = 52) -> dict[str, list[float | None]]:
    def midline(n: int) -> list[float | None]:
        out: list[float | None] = []
        for i in range(len(close)):
            h_w, l_w = high[i - n + 1:i + 1], low[i - n + 1:i + 1]
            if i < n - 1 or any(v is None for v in h_w + l_w):
                out.append(None)
                continue
            out.append((max(h_w) + min(l_w)) / 2.0)
        return out

    tenkan, kijun = midline(conv), midline(base)
    span_a_raw = [(t + k) / 2.0 if t is not None and k is not None else None
                  for t, k in zip(tenkan, kijun)]

    def shift(series: list[float | None], k: int) -> list[float | None]:
        return [series[i - k] if i >= k else None for i in range(len(series))]

    span_a = shift(span_a_raw, base)
    span_b = shift(midline(spanb), base)
    chikou = [close[i + base] if i + base < len(close) else None
              for i in range(len(close))]
    return {"tenkan": tenkan, "kijun": kijun, "span_a": span_a,
            "span_b": span_b, "chikou": chikou}


def vortex(high: list[float | None], low: list[float | None],
           close: list[float | None], n: int = 14) -> tuple[list, list]:
    size = len(close)
    vi_p: list[float | None] = [None] * size
    vi_m: list[float | None] = [None] * size
    vm_p: list[float | None] = [None] * size
    vm_m: list[float | None] = [None] * size
    trs = _true_range(high, low, close)
    for i in range(1, size):
        h, l, ph, pl = high[i], low[i], high[i - 1], low[i - 1]
        if None in (h, l, ph, pl):
            continue
        vm_p[i], vm_m[i] = abs(h - pl), abs(l - ph)
    for i in range(n, size):
        wp, wm, wt = vm_p[i - n + 1:i + 1], vm_m[i - n + 1:i + 1], \
            trs[i - n + 1:i + 1]
        if any(v is None for v in wp + wm + wt):
            continue
        t = sum(wt)
        if t:
            vi_p[i], vi_m[i] = sum(wp) / t, sum(wm) / t
    return vi_p, vi_m


def trix(close: list[float | None], n: int = 15) -> list[float | None]:
    e3 = _ema(_ema(_ema(close, n), n), n)
    out: list[float | None] = [None] * len(e3)
    for i in range(1, len(e3)):
        a, b = e3[i], e3[i - 1]
        if a is not None and b is not None and b != 0:
            out[i] = 100.0 * (a / b - 1.0)
    return out


# ---- momentum ------------------------------------------------------------------

def rsi_series(close: list[float | None], n: int = 14) -> list[float | None]:
    deltas: list[float | None] = [None]
    for i in range(1, len(close)):
        a, b = close[i - 1], close[i]
        deltas.append(None if a is None or b is None else b - a)
    gains = [None if d is None else max(d, 0.0) for d in deltas]
    losses = [None if d is None else max(-d, 0.0) for d in deltas]
    ag, al = _wilder(gains, n), _wilder(losses, n)
    out: list[float | None] = [None] * len(close)
    for i, (g, l) in enumerate(zip(ag, al)):
        if g is None or l is None:
            continue
        out[i] = 100.0 if l == 0 else (100.0 - 100.0 / (1.0 + g / l))
    return out


def stochastic(high: list[float | None], low: list[float | None],
               close: list[float | None], n: int = 14, d_n: int = 3
               ) -> tuple[list, list]:
    k: list[float | None] = [None] * len(close)
    for i in range(n - 1, len(close)):
        hw, lw, c = high[i - n + 1:i + 1], low[i - n + 1:i + 1], close[i]
        if any(v is None for v in hw + lw) or c is None:
            continue
        hh, ll = max(hw), min(lw)
        k[i] = 50.0 if hh == ll else 100.0 * (c - ll) / (hh - ll)
    return k, _sma(k, d_n)


def stoch_rsi(close: list[float | None], rsi_n: int = 14, stoch_n: int = 14,
              k_n: int = 3, d_n: int = 3) -> tuple[list, list]:
    r = rsi_series(close, rsi_n)
    raw: list[float | None] = [None] * len(r)
    for i in range(stoch_n - 1, len(r)):
        w = r[i - stoch_n + 1:i + 1]
        if any(v is None for v in w):
            continue
        hi, lo = max(w), min(w)
        raw[i] = 50.0 if hi == lo else 100.0 * (r[i] - lo) / (hi - lo)
    k = _sma(raw, k_n)
    return k, _sma(k, d_n)


def roc(close: list[float | None], n: int = 10) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        a, b = close[i - n], close[i]
        if a is not None and b is not None and a > 0:
            out[i] = 100.0 * (b / a - 1.0)
    return out


def williams_r(high: list[float | None], low: list[float | None],
               close: list[float | None], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    for i in range(n - 1, len(close)):
        hw, lw, c = high[i - n + 1:i + 1], low[i - n + 1:i + 1], close[i]
        if any(v is None for v in hw + lw) or c is None:
            continue
        hh, ll = max(hw), min(lw)
        out[i] = -50.0 if hh == ll else -100.0 * (hh - c) / (hh - ll)
    return out


def cci(high: list[float | None], low: list[float | None],
        close: list[float | None], n: int = 20) -> list[float | None]:
    tp = [None if None in (h, l, c) else (h + l + c) / 3.0
          for h, l, c in zip(high, low, close)]

    def one(w: list[float]) -> float:
        m = sum(w) / len(w)
        md = sum(abs(v - m) for v in w) / len(w)
        if md == 0:
            return 0.0
        return (w[-1] - m) / (0.015 * md)

    return _windowed(tp, n, one)


def momentum_line(close: list[float | None], n: int = 10) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        a, b = close[i - n], close[i]
        if a is not None and b is not None and a > 0:
            out[i] = b - a
    return out


def tsi(close: list[float | None], long_n: int = 25, short_n: int = 13
        ) -> list[float | None]:
    mom = [None if i == 0 or close[i] is None or close[i - 1] is None
           else close[i] - close[i - 1] for i in range(len(close))]
    num = _ema(_ema(mom, long_n), short_n)
    den = _ema(_ema([None if v is None else abs(v) for v in mom], long_n),
               short_n)
    return [None if nn is None or dd in (None, 0) else 100.0 * nn / dd
            for nn, dd in zip(num, den)]


def ultimate_oscillator(high: list[float | None], low: list[float | None],
                        close: list[float | None]) -> list[float | None]:
    size = len(close)
    bp: list[float | None] = [None] * size
    tr: list[float | None] = [None] * size
    for i in range(1, size):
        h, l, c, pc = high[i], low[i], close[i], close[i - 1]
        if None in (h, l, c, pc):
            continue
        bp[i] = c - min(l, pc)
        tr[i] = max(h, pc) - min(l, pc)

    def avg(i: int, n: int) -> float | None:
        wb, wt = bp[i - n + 1:i + 1], tr[i - n + 1:i + 1]
        if i < n - 1 or any(v is None for v in wb + wt):
            return None
        st = sum(wt)
        return sum(wb) / st if st else None

    out: list[float | None] = [None] * size
    for i in range(size):
        a7, a14, a28 = avg(i, 7), avg(i, 14), avg(i, 28)
        if None in (a7, a14, a28):
            continue
        out[i] = 100.0 * (4.0 * a7 + 2.0 * a14 + a28) / 7.0
    return out


def awesome_oscillator(high: list[float | None], low: list[float | None]
                       ) -> list[float | None]:
    median = [None if None in (h, l) else (h + l) / 2.0
              for h, l in zip(high, low)]
    fast, slow = _sma(median, 5), _sma(median, 34)
    return [None if a is None or b is None else a - b
            for a, b in zip(fast, slow)]


# ---- volatility ----------------------------------------------------------------

def bollinger(close: list[float | None], n: int = 20,
              ndev: float = 2.0) -> tuple[list, list, list, list, list]:
    """upper, mid, lower, percent-B (%B), bandwidth (population sigma)."""
    mid = _sma(close, n)
    upper: list[float | None] = [None] * len(close)
    lower: list[float | None] = [None] * len(close)
    pctb: list[float | None] = [None] * len(close)
    width: list[float | None] = [None] * len(close)
    for i in range(n - 1, len(close)):
        m = mid[i]
        w = close[i - n + 1:i + 1]
        if m is None or any(v is None for v in w):
            continue
        sd = _std_pop(w)
        u, l = m + ndev * sd, m - ndev * sd
        upper[i], lower[i] = u, l
        width[i] = (u - l) / m if m else None
        if u != l:
            pctb[i] = (close[i] - l) / (u - l)
    return upper, mid, lower, pctb, width


def atr_series(high: list[float | None], low: list[float | None],
               close: list[float | None], n: int = 14) -> list[float | None]:
    return _wilder(_true_range(high, low, close), n)


def keltner(high: list[float | None], low: list[float | None],
            close: list[float | None], n: int = 20, mult: float = 2.0
            ) -> tuple[list, list, list]:
    mid = _ema(close, n)
    a = atr_series(high, low, close, 10)
    upper = [None if m is None or aa is None else m + mult * aa
             for m, aa in zip(mid, a)]
    lower = [None if m is None or aa is None else m - mult * aa
             for m, aa in zip(mid, a)]
    return upper, mid, lower


def hist_volatility(close: list[float | None], n: int = 20
                    ) -> list[float | None]:
    logs: list[float | None] = [None]
    for i in range(1, len(close)):
        a, b = close[i - 1], close[i]
        logs.append(None if None in (a, b) or a <= 0 or b <= 0
                    else math.log(b / a))

    def one(w: list[float]) -> float:
        m = sum(w) / len(w)
        var = sum((v - m) ** 2 for v in w) / (len(w) - 1)
        return math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0

    return _windowed(logs, n, one)


def donchian_channels(high: list[float | None], low: list[float | None],
                      n: int = 20) -> tuple[list, list, list]:
    size = len(high)
    up: list[float | None] = [None] * size
    lo: list[float | None] = [None] * size
    mid: list[float | None] = [None] * size
    for i in range(n - 1, size):
        hw, lw = high[i - n + 1:i + 1], low[i - n + 1:i + 1]
        if any(v is None for v in hw + lw):
            continue
        up[i], lo[i] = max(hw), min(lw)
        mid[i] = (up[i] + lo[i]) / 2.0
    return up, mid, lo


def ulcer_index(close: list[float | None], n: int = 14) -> list[float | None]:
    def one(w: list[float]) -> float:
        peak = -math.inf
        sq: list[float] = []
        for v in w:
            peak = max(peak, v)
            dd = 100.0 * (v / peak - 1.0)
            sq.append(dd * dd)
        return math.sqrt(sum(sq) / len(sq))

    return _windowed(close, n, one)


# ---- volume --------------------------------------------------------------------

def obv_series(close: list[float | None], volume: list[float | None]
               ) -> list[float | None]:
    out: list[float | None] = [0.0 if close and close[0] is not None else None]
    for i in range(1, len(close)):
        c, pc, v = close[i], close[i - 1], volume[i]
        prev = out[-1]
        if c is None or pc is None or v is None:
            out.append(prev)
            continue
        if c > pc:
            out.append(prev + v)
        elif c < pc:
            out.append(prev - v)
        else:
            out.append(prev)
    return out


def mfi_series(high: list[float | None], low: list[float | None],
               close: list[float | None], volume: list[float | None],
               n: int = 14) -> list[float | None]:
    size = len(close)
    pos_flow = [0.0] * size
    neg_flow = [0.0] * size
    ok = [False] * size
    for i in range(1, size):
        h, l, c, pv, v = high[i], low[i], close[i], close[i - 1], volume[i]
        if None in (h, l, c, pv, v) or high[i - 1] is None or low[i - 1] is None:
            continue
        tp = (h + l + c) / 3.0
        ptp = (high[i - 1] + low[i - 1] + pv) / 3.0
        mf = tp * v
        if tp > ptp:
            pos_flow[i] = mf
        elif tp < ptp:
            neg_flow[i] = mf
        ok[i] = True
    out: list[float | None] = [None] * size
    for i in range(size):
        if i < n or not all(ok[j] for j in range(i - n + 1, i + 1)):
            continue
        p = sum(pos_flow[i - n + 1:i + 1])
        q = sum(neg_flow[i - n + 1:i + 1])
        if q == 0:
            out[i] = 100.0 if p > 0 else None
        else:
            out[i] = 100.0 - 100.0 / (1.0 + p / q)
    return out


def rolling_vwap(high: list[float | None], low: list[float | None],
                 close: list[float | None], volume: list[float | None],
                 n: int = 20) -> list[float | None]:
    """Rolling n-day VWAP from daily OHLC (no intraday data — documented)."""
    tp = [None if None in (h, l, c) else (h + l + c) / 3.0
          for h, l, c in zip(high, low, close)]
    out: list[float | None] = [None] * len(tp)
    for i in range(n - 1, len(tp)):
        wt = tp[i - n + 1:i + 1]
        wv = volume[i - n + 1:i + 1]
        if any(v is None or vv is None for v, vv in zip(wt, wv)):
            continue
        sv = sum(wv)
        out[i] = sum(t * v for t, v in zip(wt, wv)) / sv if sv else None
    return out


def cmf_series(high: list[float | None], low: list[float | None],
               close: list[float | None], volume: list[float | None],
               n: int = 20) -> list[float | None]:
    """Chaikin Money Flow: sum(MFV)/sum(volume) over n days."""
    size = len(close)
    mfv: list[float | None] = [None] * size
    for i in range(size):
        h, l, c, v = high[i], low[i], close[i], volume[i]
        if None in (h, l, c, v):
            continue
        if h == l:
            mfv[i] = 0.0
        else:
            mfv[i] = (((c - l) - (h - c)) / (h - l)) * v
    out: list[float | None] = [None] * size
    for i in range(n - 1, size):
        w = mfv[i - n + 1:i + 1]
        wv = volume[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        sv = sum(x for x in wv if x is not None)
        if sv:
            out[i] = sum(w) / sv
    return out


def pvt_series(close: list[float | None],
               volume: list[float | None]) -> list[float | None]:
    out: list[float | None] = [0.0 if close and close[0] is not None else None]
    for i in range(1, len(close)):
        c, pc, v = close[i], close[i - 1], volume[i]
        prev = out[-1]
        if None in (c, pc, v) or pc == 0:
            out.append(prev)
            continue
        out.append(prev + v * (c - pc) / pc)
    return out


def eom_series(high: list[float | None], low: list[float | None],
               close: list[float | None], volume: list[float | None],
               n: int = 14) -> list[float | None]:
    size = len(close)
    raw: list[float | None] = [None] * size
    for i in range(1, size):
        h, l, ph, pl, v = high[i], low[i], high[i - 1], low[i - 1], volume[i]
        if None in (h, l, ph, pl, v) or h == l:
            continue
        dm = (h + l) / 2.0 - (ph + pl) / 2.0
        box = (v / 10000.0) / (h - l)
        raw[i] = dm / box if box else None
    return _sma(raw, n)


# ---- structure / levels --------------------------------------------------------

def pivot_points(high: float, low: float, close: float) -> dict[str, float]:
    p = (high + low + close) / 3.0
    rng = high - low
    return {
        "pivot": p,
        "r1": 2 * p - low, "s1": 2 * p - high,
        "r2": p + rng, "s2": p - rng,
        "r3": high + 2 * (p - low), "s3": low - 2 * (high - p),
    }


def fibonacci_levels(high: float, low: float,
                     uptrend: bool = True) -> dict[str, float]:
    rng = high - low
    ratios = (("23.6%", 0.236), ("38.2%", 0.382), ("50%", 0.5),
              ("61.8%", 0.618), ("78.6%", 0.786))
    return {name: (high - r * rng if uptrend else low + r * rng)
            for name, r in ratios}


def swing_levels(high: list[float | None], low: list[float | None],
                 k: int = 5, lookback: int = 250) -> dict[str, list[float]]:
    """Fractal swing highs/lows (k bars each side) over the trailing lookback."""
    highs: list[float] = []
    lows: list[float] = []
    for i in range(max(k, len(high) - lookback), len(high) - k):
        hw = high[i - k:i + k + 1]
        lw = low[i - k:i + k + 1]
        if any(v is None for v in hw + lw):
            continue
        others_h = hw[:k] + hw[k + 1:]
        others_l = lw[:k] + lw[k + 1:]
        if high[i] > max(others_h):
            highs.append(high[i])
        if low[i] < min(others_l):
            lows.append(low[i])
    return {"resistance": sorted(set(highs), reverse=True)[:8],
            "support": sorted(set(lows))[:8]}


def week52_position(close: list[float | None]) -> dict | None:
    tail = [v for v in close[-252:] if v is not None]
    if len(tail) < 60:
        return None
    hi, lo = max(tail), min(tail)
    last = tail[-1]
    rng = hi - lo
    return {"high_52w": hi, "low_52w": lo,
            "pct_from_high": round(100.0 * (last / hi - 1.0), 2) if hi else None,
            "pct_from_low": round(100.0 * (last / lo - 1.0), 2) if lo else None,
            "position_pct": round(100.0 * (last - lo) / rng, 1) if rng else None}


# ---- candlestick patterns ------------------------------------------------------

_BULL_PATTERNS = frozenset((
    "hammer", "inverted_hammer", "bullish_engulfing", "morning_star",
    "three_white_soldiers", "piercing_line"))


def _uptrend(close: list[float | None], i: int, n: int = 5) -> bool:
    seg = [v for v in close[max(0, i - n):i + 1] if v is not None]
    return len(seg) >= 3 and seg[-1] > seg[0]


def _downtrend(close: list[float | None], i: int, n: int = 5) -> bool:
    seg = [v for v in close[max(0, i - n):i + 1] if v is not None]
    return len(seg) >= 3 and seg[-1] < seg[0]


def candlestick_patterns(open_: list[float | None], high: list[float | None],
                         low: list[float | None],
                         close: list[float | None], scan: int = 250
                         ) -> list[dict]:
    found: list[dict] = []
    start = max(2, len(close) - scan)
    for i in range(start, len(close)):
        o, h, l, c = open_[i], high[i], low[i], close[i]
        po, pc = open_[i - 1], close[i - 1]
        p2o, p2c = open_[i - 2], close[i - 2]
        if None in (o, h, l, c, po, pc):
            continue
        body = abs(c - o)
        rng = h - l
        upper = h - max(o, c)
        lower = min(o, c) - l
        green, red = c > o, c < o
        pgreen, pred = pc > po, pc < po
        pbody = abs(pc - po)
        tags: list[tuple[str, str]] = []
        if rng > 0:
            if body <= 0.1 * rng:
                tags.append(("doji", "neutral"))
            if body > 0 and lower >= 2 * body and upper <= 0.35 * body:
                if _downtrend(close, i):
                    tags.append(("hammer", "bullish"))
                else:
                    tags.append(("hanging_man", "bearish"))
            if body > 0 and upper >= 2 * body and lower <= 0.35 * body:
                if _uptrend(close, i):
                    tags.append(("shooting_star", "bearish"))
                else:
                    tags.append(("inverted_hammer", "bullish"))
        if green and pred and c >= po and o <= pc and body > pbody:
            tags.append(("bullish_engulfing", "bullish"))
        if red and pgreen and o >= pc and c <= po and body > pbody:
            tags.append(("bearish_engulfing", "bearish"))
        if None not in (p2o, p2c):
            p2red, p2green = p2c < p2o, p2c > p2o
            small_mid = pbody <= 0.4 * abs(p2c - p2o) if p2c != p2o else False
            if p2red and small_mid and green and c > (p2o + p2c) / 2 \
                    and _downtrend(close, i):
                tags.append(("morning_star", "bullish"))
            if pgreen and small_mid and red and c < (p2o + p2c) / 2 \
                    and _uptrend(close, i):
                tags.append(("evening_star", "bearish"))
            if green and pgreen and p2green and c > pc > p2c:
                tags.append(("three_white_soldiers", "bullish"))
            if red and pred and p2red and c < pc < p2c:
                tags.append(("three_black_crows", "bearish"))
        if pred and green and pbody > 0 and o < pc \
                and (po + pc) / 2 < c < po:
            tags.append(("piercing_line", "bullish"))
        if pgreen and red and pbody > 0 and o > pc \
                and po < c < (po + pc) / 2:
            tags.append(("dark_cloud_cover", "bearish"))
        for tag, direction in tags:
            found.append({"index": i, "pattern": tag, "direction": direction})
    return found


# ---- divergences ---------------------------------------------------------------

def _price_pivots(close: list[float | None], k: int = 5
                  ) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    for i in range(k, len(close) - k):
        w = close[i - k:i + k + 1]
        if any(v is None for v in w):
            continue
        others = w[:k] + w[k + 1:]
        if close[i] > max(others):
            highs.append(i)
        if close[i] < min(others):
            lows.append(i)
    return highs, lows


def divergence_events(close: list[float | None], rsi_l: list[float | None],
                      lookback: int = 60) -> list[dict]:
    """Regular divergences between the last two price pivots and RSI."""
    pivot_highs, pivot_lows = _price_pivots(close, 5)
    evts: list[dict] = []

    def check(pivots: list[int], bullish: bool) -> None:
        if len(pivots) < 2:
            return
        a, b = pivots[-2], pivots[-1]
        if b - a > lookback or b >= len(rsi_l) \
                or None in (close[a], close[b], rsi_l[a], rsi_l[b]):
            return
        if bullish and close[b] < close[a] and rsi_l[b] > rsi_l[a]:
            evts.append({"index": b, "kind": "regular_bullish_divergence",
                         "detail": "price lower low, RSI higher low"})
        elif not bullish and close[b] > close[a] and rsi_l[b] < rsi_l[a]:
            evts.append({"index": b, "kind": "regular_bearish_divergence",
                         "detail": "price higher high, RSI lower high"})

    check(pivot_lows, bullish=True)
    check(pivot_highs, bullish=False)
    return evts


# ---- composite score -----------------------------------------------------------

def _clip(v: float | None, lim: float) -> float | None:
    if v is None:
        return None
    return max(-1.0, min(1.0, v / lim))


def composite_score(*, close: list[float | None], sma50: list[float | None],
                    sma200: list[float | None], macd_line: list[float | None],
                    macd_sig: list[float | None], adx_v: float | None,
                    pdi_v: float | None, mdi_v: float | None,
                    st_dir: str | None, ich: dict | None, rsi_v: float | None,
                    roc_v: float | None, stoch_k: float | None,
                    cci_v: float | None, mfi_v: float | None,
                    obv_slope: float | None, cmf_v: float | None,
                    vwap_pos: float | None,
                    surge_dir: float | None) -> dict:
    c = close[-1] if close else None
    s50 = sma50[-1] if sma50 else None
    s200 = sma200[-1] if sma200 else None
    ml = macd_line[-1] if macd_line else None
    ms = macd_sig[-1] if macd_sig else None

    tvotes: list[float] = []
    if None not in (c, s50):
        tvotes.append(1.0 if c > s50 else -1.0)
    if None not in (s50, s200):
        tvotes.append(1.0 if s50 > s200 else -1.0)
    if None not in (ml, ms):
        tvotes.append(1.0 if ml > ms else -1.0)
    if ml is not None:
        tvotes.append(1.0 if ml > 0 else -1.0)
    if None not in (adx_v, pdi_v, mdi_v) and adx_v >= 20:
        tvotes.append(1.0 if pdi_v > mdi_v else -1.0)
    if st_dir in (BULLISH, BEARISH):
        tvotes.append(1.0 if st_dir == BULLISH else -1.0)
    if ich:
        kj = ich.get("kijun")
        kk = kj[-1] if kj else None
        sa_l = ich.get("span_a")
        sb_l = ich.get("span_b")
        sa = sa_l[-1] if sa_l else None
        sb = sb_l[-1] if sb_l else None
        if None not in (c, sa, sb):
            top, bot = max(sa, sb), min(sa, sb)
            tvotes.append(1.0 if c > top else -1.0 if c < bot else 0.0)
        elif None not in (c, kk):
            tvotes.append(1.0 if c > kk else -1.0)
    trend_score = 100.0 * (sum(tvotes) / len(tvotes)) if tvotes else None

    mvotes: list[float] = []
    for cand, lim in (
            (None if rsi_v is None else (rsi_v - 50.0), 25.0),
            (roc_v, 15.0),
            (None if stoch_k is None else (stoch_k - 50.0), 40.0),
            (cci_v, 150.0),
            (None if mfi_v is None else (mfi_v - 50.0), 35.0)):
        x = _clip(cand, lim)
        if x is not None:
            mvotes.append(x)
    momentum_score = 100.0 * (sum(mvotes) / len(mvotes)) if mvotes else None

    vvotes: list[float] = []
    cv = _clip(cmf_v, 0.25)
    if cv is not None:
        vvotes.append(min(1.0, abs(cv) * 1.5) * (1 if cv > 0 else -1))
    if vwap_pos is not None:
        vvotes.append(vwap_pos)
    if surge_dir is not None:
        vvotes.append(surge_dir)
    if obv_slope is not None:
        vvotes.append(1.0 if obv_slope > 0 else (-1.0 if obv_slope < 0 else 0.0))
    volume_score = 100.0 * (sum(vvotes) / len(vvotes)) if vvotes else None

    parts = [(trend_score, 0.40), (momentum_score, 0.35), (volume_score, 0.25)]
    total = sum(s * wgt for s, wgt in parts if s is not None)
    wsum = sum(wgt for s, wgt in parts if s is not None)
    composite = round(total / wsum) if wsum else None
    bias = None
    if composite is not None:
        bias = ("bullish" if composite >= 25
                else "bearish" if composite <= -25 else "neutral")
    return {
        "composite": composite, "bias": bias,
        "components": {
            "trend": None if trend_score is None else round(trend_score),
            "momentum": None if momentum_score is None else round(momentum_score),
            "volume": None if volume_score is None else round(volume_score)},
    }


# ---- orchestrator ---------------------------------------------------------------

def _latest(series: list | None):
    return series[-1] if series else None


def _sig_from_value(v: float | None, bear_hi_inclusive: float,
                    bull_lo_inclusive: float) -> str | None:
    """Band reader: v <= bear_hi -> bearish, v >= bull_lo -> bullish."""
    if v is None:
        return None
    if v <= bear_hi_inclusive:
        return BEARISH
    if v >= bull_lo_inclusive:
        return BULLISH
    return NEUTRAL


def _rsi_zone_events(rsi_l: list[float | None]) -> list[tuple[int, dict]]:
    evts: list[tuple[int, dict]] = []
    prev_zone = ""
    for i, v in enumerate(rsi_l):
        if v is None:
            continue
        zone = "overbought" if v >= 70 else "oversold" if v <= 30 else "mid"
        if prev_zone and zone != prev_zone and zone == "mid":
            evts.append((i, {
                "kind": "rsi_exit_" + prev_zone,
                "direction": "up" if prev_zone == "oversold" else "down",
                "detail": f"RSI exited {prev_zone} zone ({round(v, 1)})"}))
        prev_zone = zone
    return evts


OVERLAY_SERIES = (
    "dates", "open", "high", "low", "close", "volume",
    "sma_20", "sma_50", "sma_200", "ema_12", "ema_26",
    "bb_upper", "bb_lower", "rsi_14", "macd", "macd_signal",
    "macd_hist", "supertrend_level",
)


def compute_technical(data: dict, window: int = 260) -> dict:
    """Full technical payload for one stock from aligned OHLCV arrays."""
    dates = list(data.get("dates") or [])
    close = list(data.get("close") or [])
    open_ = list(data.get("open") or [None] * len(close))
    high = list(data.get("high") or [None] * len(close))
    low = list(data.get("low") or [None] * len(close))
    volume = list(data.get("volume") or [None] * len(close))
    n = len(close)
    payload: dict = {"methodology_version": TECH_VERSION,
                     "as_of": dates[-1] if dates else None,
                     "points": n, "indicators": {},
                     "signals": [], "patterns": [], "series": {}}
    if n < 30:
        payload["error"] = INSUFFICIENT
        return payload

    sma20 = sma_series(close, 20)
    sma50 = sma_series(close, 50)
    sma200 = sma_series(close, 200)
    ema12 = ema_series(close, 12)
    ema26 = ema_series(close, 26)
    macd_l, macd_s, macd_h = macd(close)
    adx_l, pdi, mdi = adx(high, low, close)
    st = supertrend(high, low, close)
    ar_up, ar_dn = aroon(high, low)
    ich = ichimoku(high, low, close)
    vi_p, vi_m = vortex(high, low, close)
    trix_l = trix(close)
    psar = parabolic_sar(high, low)
    rsi_l = rsi_series(close)
    stoch_k, stoch_d = stochastic(high, low, close)
    srsi_k, srsi_d = stoch_rsi(close)
    roc_l = roc(close)
    wr_l = williams_r(high, low, close)
    cci_l = cci(high, low, close)
    mom_l = momentum_line(close)
    tsi_l = tsi(close)
    uo_l = ultimate_oscillator(high, low, close)
    ao_l = awesome_oscillator(high, low)
    bb_u, bb_m, bb_l, bb_pb, bb_w = bollinger(close)
    atr_l = atr_series(high, low, close)
    kc_u, kc_m, kc_l = keltner(high, low, close)
    hv20 = hist_volatility(close, 20)
    hv60 = hist_volatility(close, 60)
    dc_u, dc_m, dc_d = donchian_channels(high, low)
    ui_l = ulcer_index(close)
    obv_l = obv_series(close, volume)
    mfi_l = mfi_series(high, low, close, volume)
    vwap_l = rolling_vwap(high, low, close, volume)
    cmf_l = cmf_series(high, low, close, volume)
    pvt_l = pvt_series(close, volume)
    vol_sma = _sma(list(volume), 20)
    eom_l = eom_series(high, low, close, volume)

    cl = _latest(close)
    ind = payload["indicators"]
    ind["trend"] = {}
    for name, series in (("sma_20", sma20), ("sma_50", sma50),
                         ("sma_200", sma200), ("ema_12", ema12),
                         ("ema_26", ema26)):
        v = _latest(series)
        sig = None
        if v is not None and cl is not None:
            sig = BULLISH if cl > v else BEARISH
        ind["trend"][name] = {"value": v, "signal": sig}
    ml, msig, mh = _latest(macd_l), _latest(macd_s), _latest(macd_h)
    ind["trend"]["macd"] = {
        "value": ml, "signal_line": msig, "histogram": mh,
        "signal": NEUTRAL if None in (ml, msig)
                  else BULLISH if ml > msig else BEARISH}
    av, pv_, mv_ = _latest(adx_l), _latest(pdi), _latest(mdi)
    ind["trend"]["adx"] = {
        "value": av, "plus_di": pv_, "minus_di": mv_,
        "trending": bool(av is not None and av >= 25),
        "signal": (NEUTRAL if None in (av, pv_, mv_) or av < 20
                   else BULLISH if pv_ > mv_ else BEARISH)}
    st_now = st[-1] if st else ("neutral", None)
    ind["trend"]["supertrend"] = {
        "value": st_now[1], "direction": st_now[0],
        "signal": st_now[0] if st_now[0] in (BULLISH, BEARISH) else None}
    ps = _latest(psar)
    ind["trend"]["psar"] = {
        "value": ps,
        "signal": (None if None in (ps, cl) else
                   BULLISH if cl > ps else BEARISH)}
    au, ad_ = _latest(ar_up), _latest(ar_dn)
    ind["trend"]["aroon"] = {
        "up": au, "down": ad_,
        "signal": (NEUTRAL if None in (au, ad_)
                   else BULLISH if au > 70 and au > ad_
                   else BEARISH if ad_ > 70 and ad_ > au else NEUTRAL)}
    vp, vm = _latest(vi_p), _latest(vi_m)
    ind["trend"]["vortex"] = {
        "vi_plus": vp, "vi_minus": vm,
        "signal": (NEUTRAL if None in (vp, vm)
                   else BULLISH if vp > vm else BEARISH)}
    tx = _latest(trix_l)
    ind["trend"]["trix"] = {"value": tx,
                            "signal": _sig_from_value(tx, 0.0, 0.0)
                            if tx is not None and tx != 0 else
                            NEUTRAL if tx == 0 else None}
    tk = _latest(ich["tenkan"])
    kj = _latest(ich["kijun"])
    sa = _latest(ich["span_a"])
    sb = _latest(ich["span_b"])
    cloud_top = max(sa, sb) if None not in (sa, sb) else None
    cloud_bot = min(sa, sb) if None not in (sa, sb) else None
    ich_sig = (NEUTRAL if None in (tk, kj, cl) else
               BULLISH if cl > max(tk, kj) and
               (cloud_top is None or cl > cloud_top) else
               BEARISH if cl < min(tk, kj) and
               (cloud_bot is None or cl < cloud_bot) else NEUTRAL)
    ind["trend"]["ichimoku"] = {"tenkan": tk, "kijun": kj,
                                "span_a": sa, "span_b": sb,
                                "signal": ich_sig}

    rv = _latest(rsi_l)
    ind["momentum"] = {
        "rsi_14": {"value": rv, "signal": _sig_from_value(rv, 30.0, 55.0)},
        "stoch_fast": {"k": _latest(stoch_k), "d": _latest(stoch_d)},
        "stoch_rsi": {"k": _latest(srsi_k), "d": _latest(srsi_d)},
        "roc_10": {"value": _latest(roc_l),
                   "signal": (NEUTRAL if _latest(roc_l) is None else
                              BULLISH if _latest(roc_l) > 0 else BEARISH)},
        "williams_r": {"value": _latest(wr_l),
                       "signal": _sig_from_value(_latest(wr_l), -80.0, -50.0)},
        "cci_20": {"value": _latest(cci_l),
                   "signal": _sig_from_value(_latest(cci_l), -100.0, 100.0)},
        "momentum_10": {"value": _latest(mom_l)},
        "tsi": {"value": _latest(tsi_l),
                "signal": (NEUTRAL if _latest(tsi_l) is None else
                           BULLISH if _latest(tsi_l) > 0 else BEARISH)},
        "ultimate_osc": {"value": _latest(uo_l),
                         "signal": _sig_from_value(_latest(uo_l), 40.0, 60.0)},
        "awesome_osc": {"value": _latest(ao_l),
                        "signal": (NEUTRAL if _latest(ao_l) is None else
                                   BULLISH if _latest(ao_l) > 0 else BEARISH)},
    }

    pb = _latest(bb_pb)
    ind["volatility"] = {
        "bollinger_20_2": {
            "upper": _latest(bb_u), "mid": _latest(bb_m),
            "lower": _latest(bb_l), "percent_b": pb,
            "bandwidth": _latest(bb_w),
            "signal": (NEUTRAL if pb is None else
                       BULLISH if pb >= 1.0 else
                       BEARISH if pb <= 0.0 else NEUTRAL)},
        "atr_14": {
            "value": _latest(atr_l),
            "atr_pct": (round(100.0 * _latest(atr_l) / cl, 2)
                        if _latest(atr_l) is not None and cl else None)},
        "keltner": {"upper": _latest(kc_u), "mid": _latest(kc_m),
                    "lower": _latest(kc_l)},
        "hist_volatility_20d": {"value": _latest(hv20)},
        "hist_volatility_60d": {"value": _latest(hv60)},
        "donchian_20": {"upper": _latest(dc_u), "mid": _latest(dc_m),
                        "lower": _latest(dc_d)},
        "ulcer_index_14": {"value": _latest(ui_l)},
    }

    vol_last, vol_avg = _latest(volume), _latest(vol_sma)
    vsurge = vol_last / vol_avg if (vol_last is not None and vol_avg) else None
    vw = _latest(vwap_l)
    vwap_pos = None if None in (vw, cl) else (1.0 if cl > vw else -1.0)
    surge_dir = None
    if vsurge is not None and vsurge >= 1.5 and cl is not None and n >= 2 \
            and close[-2] is not None:
        surge_dir = 1.0 if cl > close[-2] else -1.0
    ov_now = _latest(obv_l)
    ov_past = obv_l[-21] if len(obv_l) >= 21 else None
    obv_slope = (ov_now - ov_past) if None not in (ov_now, ov_past) else None
    ind["volume"] = {
        "obv": {"value": ov_now, "slope_20d": obv_slope},
        "mfi_14": {"value": _latest(mfi_l),
                   "signal": _sig_from_value(_latest(mfi_l), 20.0, 55.0)},
        "rolling_vwap_20d": {
            "value": vw,
            "signal": (NEUTRAL if vwap_pos is None
                       else BULLISH if vwap_pos > 0 else BEARISH)},
        "cmf_20": {"value": _latest(cmf_l),
                   "signal": _sig_from_value(_latest(cmf_l), -0.05, 0.05)},
        "pvt": {"value": _latest(pvt_l)},
        "volume_sma_20": {"value": vol_avg,
                          "surge_ratio": round(vsurge, 2) if vsurge else None},
        "eom_14": {"value": _latest(eom_l)},
    }
    w52 = week52_position(close)
    structure: dict = {}
    if w52:
        structure["week_52"] = w52

    # ---- events -----------------------------------------------------------------
    events: list[dict] = []
    for i, kind in _cross_events(sma50, sma200):
        events.append({"_idx": i, "date": dates[i],
                       "kind": "golden_cross" if kind == "up" else "death_cross",
                       "direction": kind, "detail": "SMA50 crossed SMA200"})
    for i, kind in _cross_events(macd_l, macd_s):
        events.append({"_idx": i, "date": dates[i], "kind": "macd_cross",
                       "direction": kind,
                       "detail": "MACD crossed its signal line"})
    for i, evt in _rsi_zone_events(rsi_l):
        events.append({"_idx": i, "date": dates[i], **evt})
    prev_dir = None
    for i in range(len(st)):
        d, _lvl = st[i]
        if d in (BULLISH, BEARISH):
            if prev_dir is not None and d != prev_dir:
                events.append({
                    "_idx": i, "date": dates[i], "kind": "supertrend_flip",
                    "direction": "up" if d == BULLISH else "down",
                    "detail": f"Supertrend flipped {d}"})
            prev_dir = d
    for e in divergence_events(close, rsi_l):
        events.append({"_idx": e["index"], "date": dates[e["index"]],
                       "kind": e["kind"],
                       "direction": "up" if "bullish" in e["kind"] else "down",
                       "detail": e["detail"]})
    events = [e for e in events if e.get("date")]
    events.sort(key=lambda e: e["_idx"])
    payload["signals"] = [{k: v for k, v in e.items() if k != "_idx"}
                          for e in events[-40:]]

    pats = candlestick_patterns(open_, high, low, close)
    for p in pats:
        p["date"] = dates[p.pop("index")]
    payload["patterns"] = pats[-40:]

    lv = swing_levels(high, low)
    structure["support"] = lv["support"]
    structure["resistance"] = lv["resistance"]
    pp_prev = None
    for i in range(len(close) - 1, -1, -1):
        if None not in (high[i], low[i], close[i]):
            pp_prev = pivot_points(high[i], low[i], close[i])
            break
    if pp_prev:
        structure["pivot_points_next"] = pp_prev
    tail_h = [v for v in high[-120:] if v is not None]
    tail_l = [v for v in low[-120:] if v is not None]
    if tail_h and tail_l:
        fib_hi, fib_lo = max(tail_h), min(tail_l)
        up = bool(cl is not None and cl >= (fib_hi + fib_lo) / 2.0)
        structure["fibonacci_120d"] = {"swing_high": fib_hi,
                                       "swing_low": fib_lo,
                                       "uptrend": up,
                                       "levels": fibonacci_levels(
                                           fib_hi, fib_lo, uptrend=up)}
    ind["structure"] = structure

    payload["score"] = composite_score(
        close=close, sma50=sma50, sma200=sma200, macd_line=macd_l,
        macd_sig=macd_s, adx_v=_latest(adx_l), pdi_v=_latest(pdi),
        mdi_v=_latest(mdi), st_dir=st_now[0] if st_now[0] in (BULLISH,
                                                              BEARISH) else None,
        ich=ich, rsi_v=rv, roc_v=_latest(roc_l), stoch_k=_latest(stoch_k),
        cci_v=_latest(cci_l), mfi_v=_latest(mfi_l), obv_slope=obv_slope,
        cmf_v=_latest(cmf_l), vwap_pos=vwap_pos, surge_dir=surge_dir)

    w = max(0, int(window))
    payload["series"] = {
        "dates": dates[-w:], "open": open_[-w:], "high": high[-w:],
        "low": low[-w:], "close": close[-w:], "volume": volume[-w:],
        "sma_20": sma20[-w:], "sma_50": sma50[-w:], "sma_200": sma200[-w:],
        "ema_12": ema12[-w:], "ema_26": ema26[-w:],
        "bb_upper": bb_u[-w:], "bb_lower": bb_l[-w:],
        "rsi_14": rsi_l[-w:], "macd": macd_l[-w:],
        "macd_signal": macd_s[-w:], "macd_hist": macd_h[-w:],
        "supertrend_level": [x[1] for x in st][-w:],
    }
    return payload
