"""Performance & risk analytics helpers.

Pure functions, no I/O — every figure here is unit-tested against
hand-computed values (PLAN_PERFORMANCE_ANALYTICS verification checklist).

Debt metrics [DBT5]:
    bullet_modified_duration() — Macaulay / modified duration for a fixed-rate
    bullet bond (or zero-coupon instrument when the coupon is 0/None), driven
    by an explicit yield-to-maturity or a clean price (YTM solved by
    bisection). Yields are effective-annual throughout.

Scheme performance metrics [ANA1]:
    compute_series_analytics() — CAGR windows, annualised volatility,
    Sharpe / Sortino, max drawdown and a 1Y rolling-return distribution from
    a dated NAV series; beta / alpha / tracking error / information ratio
    when a benchmark return series is supplied.

Compliance: figures are factual computations over public AMFI NAV data.
Every output carries its as-of date and computation window; callers must
render the standard "past performance" disclaimer beside them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# Conventions (documented on the methodology page):
TRADING_DAYS = 252            # annualisation factor for daily stats
DAYS_PER_YEAR = 365.25        # calendar-day CAGR exponent
DEFAULT_RF_PCT = 6.0          # documented assumption until a T-bill feed lands
ROLLING_WINDOW_YEARS = 1      # rolling-return window
MIN_POINTS_FOR_STATS = 30     # below this, risk metrics are None (honest gaps)
MIN_CAGR_WINDOW_DAYS = 90     # below this, even since-inception CAGR is None:
                              # annualising a few days fabricates absurd rates
MIN_RISK_WINDOW_DAYS = 365    # risk stats need >=1y of observed span, not just
                              # >=30 points: 30 points over 5 weeks must never
                              # masquerade as "3-year" volatility
METHODOLOGY_VERSION = "perf-v1.2-2026-08-24"  # stamped into proposals [ANA4]


def parse_nav_date(s) -> date | None:
    """AMFI 'DD-Mon-YYYY' or ISO 'YYYY-MM-DD' (or a date object) -> date."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((str(s) if s else "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def daily_returns(values: list[float]) -> list[float]:
    out = []
    for a, b in zip(values, values[1:]):
        if a and a > 0 and b and b > 0:
            out.append(b / a - 1.0)
    return out


def cagr_between(start_v: float, end_v: float, days: int) -> float | None:
    if days <= 0 or start_v <= 0 or end_v <= 0:
        return None
    return (end_v / start_v) ** (DAYS_PER_YEAR / days) - 1.0


def _window(series: list[tuple[date, float]], cutoff: date
            ) -> list[tuple[date, float]] | None:
    pts = [(d, v) for d, v in series if v is not None and d >= cutoff]
    if len(pts) < 2 or pts[0][1] <= 0:
        return None
    return pts


def _window_cagr_info(series: list[tuple[date, float]], years: float,
                      today: date) -> tuple[float | None, dict | None]:
    """(CAGR fraction, window metadata) for an N-year slice of ``series``.

    The metadata is returned even when the CAGR is an honest null, so callers
    can show WHICH dates were considered (e.g. "2026-08-17 -> 2026-08-21,
    incomplete") next to the em-dash."""
    req = round(years * DAYS_PER_YEAR)
    cutoff = today - timedelta(days=req)
    pts = [(d, v) for d, v in series if v is not None and d >= cutoff]
    if len(pts) < 2 or pts[0][1] <= 0:
        return None, None
    (d0, v0), (d1, v1) = pts[0], pts[-1]
    info = {"start": d0.isoformat(), "end": d1.isoformat(),
            "days": (d1 - d0).days, "points": len(pts),
            "complete": (d1 - d0).days >= req - 5}
    # Honest windows only: a fund younger than the requested window gets null,
    # never its since-inception figure masquerading as a 3Y/5Y number.
    val = None
    if info["complete"]:
        val = cagr_between(v0, v1, info["days"])
    return val, info


def _window_cagr(series: list[tuple[date, float]], years: float,
                 today: date) -> float | None:
    return _window_cagr_info(series, years, today)[0]


def max_drawdown(values: list[float]) -> float | None:
    peak = None
    worst = 0.0
    saw = False
    for v in values:
        if v is None or v <= 0:
            continue
        saw = True
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst if saw else None


def rolling_1y_distribution(series: list[tuple[date, float]],
                            today: date) -> dict | None:
    """% positive + spread of rolling 1Y returns at daily steps."""
    win_days = round(ROLLING_WINDOW_YEARS * DAYS_PER_YEAR)
    tol = win_days - 5  # allow short calendar gaps around month-ends
    vals = [(d, v) for d, v in series if v is not None and v > 0]
    if len(vals) < MIN_POINTS_FOR_STATS:
        return None
    rets: list[float] = []
    first_base: date | None = None
    last_end: date | None = None
    j = 0
    for d, v in vals:
        while j < len(vals) and vals[j][0] < d - timedelta(days=win_days):
            j += 1
        if j < len(vals):
            bd, bv = vals[j]
            if (d - bd).days >= tol and bv > 0:
                rets.append(v / bv - 1.0)
                if first_base is None:
                    first_base = bd
                last_end = d
    if len(rets) < MIN_POINTS_FOR_STATS:
        return None
    pos = sum(1 for r in rets if r > 0)
    rs = sorted(rets)
    return {
        "window_days": win_days,
        "first_window_start": first_base.isoformat() if first_base else None,
        "last_window_end": last_end.isoformat() if last_end else None,
        "n_periods": len(rets),
        "pct_positive": round(pos / len(rets) * 100.0, 1),
        "best_pct": round(rs[-1] * 100.0, 2),
        "worst_pct": round(rs[0] * 100.0, 2),
        "median_pct": round(rs[len(rs) // 2] * 100.0, 2),
    }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def benchmark_relative_stats(scheme_rets: dict[str, float],
                             bench_rets: dict[str, float]) -> dict | None:
    """Beta / Jensen's alpha / tracking error / IR from aligned daily returns.

    ``*_rets`` map YYYY-MM-DD -> simple daily return; only common dates count.
    Alpha is Jensen's, annualised: mean_s - beta*mean_b, x TRADING_DAYS."""
    common = sorted(set(scheme_rets) & set(bench_rets))
    if len(common) < MIN_POINTS_FOR_STATS:
        return None
    # Same honesty rule as the risk block: >=30 common days is not enough on
    # its own — the overlap must span a real period before it may be labelled.
    if (date.fromisoformat(common[-1]) - date.fromisoformat(common[0])).days \
            < MIN_RISK_WINDOW_DAYS:
        return None
    s = [scheme_rets[d] for d in common]
    b = [bench_rets[d] for d in common]
    ms, mb = _mean(s), _mean(b)
    var_b = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    if var_b <= 0:
        return None
    cov = sum((si - ms) * (bi - mb) for si, bi in zip(s, b)) / (len(b) - 1)
    beta = cov / var_b
    diff = [si - bi for si, bi in zip(s, b)]
    te = _std(diff)
    active_annual = (ms - mb) * TRADING_DAYS
    out = {"beta": round(beta, 3),
           "alpha_pct": round((ms - beta * mb) * TRADING_DAYS * 100.0, 2),
           "tracking_error_pct": round(te * (TRADING_DAYS ** 0.5) * 100.0, 2)}
    out["information_ratio"] = (round(active_annual / te, 3)
                                if te > 0 else None)
    out["n_days"] = len(common)
    out["window_start"] = common[0]
    out["window_end"] = common[-1]
    return out


def compute_series_analytics(series: list[tuple[str, float]],
                             rf_pct: float = DEFAULT_RF_PCT,
                             bench_series: list[tuple[str, float]] | None = None
                             ) -> dict:
    """Full metric suite over a dated NAV series ([ANA1]).

    ``series``: [(date_str 'DD-Mon-YYYY' or ISO, nav), ...] any order.
    Returns a JSON-ready dict with as-of date, per-window CAGRs, risk stats,
    rolling distribution and optional benchmark-relative block. Missing-data
    slots are None rather than fabricated."""
    parsed: list[tuple[date, float]] = []
    seen = {}
    for ds, v in series or []:
        d = parse_nav_date(ds)
        if d and v is not None and v > 0:
            seen[d] = v  # last value wins on duplicate dates
    parsed = sorted(seen.items())
    today = parsed[-1][0] if parsed else date.today()
    out: dict = {
        "as_of": today.isoformat(),
        "rf_pct_assumption": rf_pct,
        "points": len(parsed),
        "methodology_version": METHODOLOGY_VERSION,
        "disclaimer": "Past performance is not indicative of future returns.",
    }
    if not parsed:
        out["error"] = "no usable NAV points"
        return out

    first, last = parsed[0], parsed[-1]
    span_days = (last[0] - first[0]).days
    out["inception"] = {"date": first[0].isoformat(), "nav": first[1],
                        "years": round(span_days / DAYS_PER_YEAR, 2)}
    si = cagr_between(first[1], last[1], span_days)
    y1_val, y1_win = _window_cagr_info(parsed, 1, today)
    y3_val, y3_win = _window_cagr_info(parsed, 3, today)
    y5_val, y5_win = _window_cagr_info(parsed, 5, today)
    out["cagr_pct"] = {
        "since_inception": (round(si * 100.0, 2)
                            if si is not None and span_days >= MIN_CAGR_WINDOW_DAYS
                            else None),
        "y1": _pct_or_none(y1_val),
        "y3": _pct_or_none(y3_val),
        "y5": _pct_or_none(y5_val),
        # The exact dates each figure spans, so the UI can show WHICH history
        # was used (and, when complete=false, why the cell is an em-dash).
        "since_inception_window": {"start": first[0].isoformat(),
                                   "end": last[0].isoformat(),
                                   "days": span_days, "points": len(parsed),
                                   "complete": span_days >= MIN_CAGR_WINDOW_DAYS},
        "y1_window": y1_win,
        "y3_window": y3_win,
        "y5_window": y5_win,
    }

    cutoff3y = today - timedelta(days=round(3 * DAYS_PER_YEAR))
    recent = [(d, v) for d, v in parsed if d >= cutoff3y]
    recent_span = (recent[-1][0] - recent[0][0]).days if recent else 0
    if len(recent) >= MIN_POINTS_FOR_STATS and recent_span >= MIN_RISK_WINDOW_DAYS:
        rets = daily_returns([v for _, v in recent])
        n = len(rets)
        mean_d, std_d = _mean(rets), _std(rets)
        rf_daily = rf_pct / 100.0 / TRADING_DAYS
        downside = [min(r - rf_daily, 0.0) for r in rets]
        dd_dev = (_mean([x * x for x in downside])) ** 0.5
        vol_ann = std_d * (TRADING_DAYS ** 0.5)
        cagr3 = _window_cagr(parsed, 3, today) or 0.0
        sharpe = ((cagr3 - rf_pct / 100.0) / vol_ann
                  if vol_ann > 1e-9 else None)
        sortino = ((cagr3 - rf_pct / 100.0) /
                   (dd_dev * TRADING_DAYS ** 0.5)
                   if dd_dev > 1e-12 else None)
        out["risk"] = {
            "window_years": 3,
            "window_start": recent[0][0].isoformat(),
            "window_end": recent[-1][0].isoformat(),
            "n_points": len(recent),
            "volatility_pct": round(vol_ann * 100.0, 2) if vol_ann > 0 else None,
            "sharpe": round(sharpe, 3) if sharpe is not None else None,
            "sortino": round(sortino, 3) if sortino is not None else None,
            "max_drawdown_pct": _pct_or_none(max_drawdown([v for _, v in recent])),
        }
        out["rolling_1y"] = rolling_1y_distribution(parsed, today)
    else:
        out["risk"] = None
        out["rolling_1y"] = None
        # Machine-readable reason so the UI can state exactly which dates were
        # considered and why they fall short (honest nulls, never fabrication).
        out["risk_unavailable"] = {
            "reason": "insufficient_history",
            "required_points": MIN_POINTS_FOR_STATS,
            "required_span_days": MIN_RISK_WINDOW_DAYS,
            "window_start": cutoff3y.isoformat(),
            "window_end": today.isoformat(),
            "found_points": len(recent),
            "found_start": recent[0][0].isoformat() if recent else None,
            "found_end": recent[-1][0].isoformat() if recent else None,
        }

    if bench_series:
        bseen = {}
        for ds, v in bench_series:
            d = parse_nav_date(ds)
            if d and v and v > 0:
                bseen[d] = v
        svals = [(d, v) for d, v in parsed if d >= cutoff3y]
        sr = daily_returns([v for _, v in svals])
        sdates = [d.isoformat() for d, _ in svals[1:]]
        sret_map = dict(zip(sdates, sr))
        bsorted = sorted(bseen.items())
        bvals = [(d, v) for d, v in bsorted if d >= cutoff3y]
        br = daily_returns([v for _, v in bvals])
        bdates = [d.isoformat() for d, _ in bvals[1:]]
        bret_map = dict(zip(bdates, br))
        out["benchmark"] = benchmark_relative_stats(sret_map, bret_map)
    else:
        out["benchmark"] = None
    return out


def _pct_or_none(v: float | None) -> float | None:
    return round(v * 100.0, 2) if v is not None else None


def _pv_schedule(face: float, cpn_per_period: float, n: int,
                 y_per_period: float) -> tuple[float, float]:
    """Return (dirty price, Macaulay duration in PERIODS) for a bullet bond."""
    disc = 1.0 + y_per_period
    price = 0.0
    weighted = 0.0
    for t in range(1, n + 1):
        cf = cpn_per_period + (face if t == n else 0.0)
        pv = cf / (disc ** t)
        price += pv
        weighted += t * pv
    return price, (weighted / price if price > 0 else 0.0)


def bullet_modified_duration(*, coupon_pct: float | None, years: float,
                             ytm_pct: float | None = None, price: float | None = None,
                             face: float = 100.0, coupons_per_year: int = 1
                             ) -> dict | None:
    """Modified duration (years) of a fixed-rate bullet bond.

    Exactly one of ``ytm_pct`` / ``price`` drives the computation; with a
    price the periodic YTM is solved first (bisection). A missing or zero
    coupon is treated as a zero-coupon instrument: Macaulay duration equals
    maturity exactly (no period-grid rounding, so sub-1y T-Bills stay right).

    Returns {"modified_duration", "macaulay_duration", "ytm_pct"} or None
    when inputs are insufficient (no tenor, non-positive tenor, no yield
    source, or price unreachable within 0–200% annual yield)."""
    if years is None or years <= 0:
        return None
    freq = max(1, int(coupons_per_year or 1))
    cpn_total = (coupon_pct or 0.0) / 100.0 * face

    def zc_from_annual(y_annual: float) -> dict:
        mac = years
        return {"modified_duration": round(mac / (1.0 + y_annual), 4),
                "macaulay_duration": round(mac, 4),
                "ytm_pct": round(y_annual * 100.0, 4)}

    if not cpn_total:  # ---- zero-coupon -------------------------------------------------
        y_annual: float | None = None
        if ytm_pct is not None and ytm_pct > 0:
            y_annual = ytm_pct / 100.0
        elif price is not None and 0 < price <= face:
            # face/(1+y)^T = price  ->  y = (face/price)^(1/T) - 1
            y_annual = (face / price) ** (1.0 / years) - 1.0
        elif price is not None and price > face:
            return None  # a zero-coupon cannot trade above its face value
        if y_annual is None:
            return None
        return zc_from_annual(min(y_annual, 2.0))

    # ---- coupon-bearing -----------------------------------------------------------------
    n = max(1, round(years * freq))
    cpn = cpn_total / freq

    def price_at(y_period: float) -> float:
        p, _ = _pv_schedule(face, cpn, n, y_period)
        return p

    y_period: float | None = None
    if ytm_pct is not None and ytm_pct > 0:
        y_period = (1.0 + ytm_pct / 100.0) ** (1.0 / freq) - 1.0
    elif price is not None and price > 0:
        lo, hi = 1e-9, 3.0  # ~0% .. ~300%/period — beyond any real quote
        if price_at(hi) > price:
            return None
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if price_at(mid) > price:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-13:
                break
        y_period = (lo + hi) / 2.0
    if y_period is None:
        return None

    _, mac_periods = _pv_schedule(face, cpn, n, y_period)
    mac_years = mac_periods / freq
    y_annual_eff = (1.0 + y_period) ** freq - 1.0
    return {"modified_duration": round(mac_years / (1.0 + y_period), 4),
            "macaulay_duration": round(mac_years, 4),
            "ytm_pct": round(y_annual_eff * 100.0, 4)}
