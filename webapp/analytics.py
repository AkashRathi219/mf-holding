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

from bisect import bisect_left
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
METHODOLOGY_VERSION = "perf-v1.4-2026-08-24"  # stamped into proposals [ANA4]


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


# ---- [ANA3] cash-flow-aware portfolio movement ---------------------------------


def _tx_key(t: dict) -> tuple[str, str]:
    """Group key for a transaction record: amfi_code preferred, ISIN fallback."""
    code = (t.get("amfi_code") or "").strip()
    isin = (t.get("isin") or "").strip().upper()
    return (code, isin)


def _xirr(flows: list[tuple[date, float]]) -> float | None:
    """Money-weighted annualised return (365.25 basis) from dated cash flows.

    ``flows`` = [(date, signed amount)]; external cash out is negative,
    the terminal value is a positive amount at the end. Bisection on the
    NPV bracket [-0.99, +10.0] annual; None when no sign change."""
    if len(flows) < 2:
        return None

    def npv(r: float) -> float:
        d0 = flows[0][0]
        return sum(a / (1.0 + r) ** ((d - d0).days / DAYS_PER_YEAR)
                   for d, a in flows)

    lo, hi = -0.99, 10.0  # -99% / +1000% annual — beyond any real bracket
    if not npv(lo) * npv(hi) <= 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if npv(mid) * npv(lo) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def portfolio_movement_series(items: list[dict], transactions: list[dict],
                              nav_lookup) -> dict | None:
    """Reconstruct the portfolio's ACTUAL value path from its cash flows.

    ``items``: statement-end holdings ``[{isin, name, units, amfi_code?}]``.
    ``transactions``: canonical records from ``parse_cas_transactions``
    (signed units/amount by type; PURCHASE +, REDEEM -, SWITCH_IN/OUT ±).
    ``nav_lookup``: ``(amfi_code, isin) -> {iso_date: nav} | None``.

    Method [PLAN_PORTFOLIO_MOVEMENT]:
    1. opening_units = end_units - Σ signed tx units  (deduced, per scheme)
    2. start = earliest tx date; end = max(last tx date, last framed NAV)
    3. daily grid = union of scheme NAV dates and tx dates within [start, end];
       units_t = opening + Σ tx units <= t (step function); value = units x
       NAV (forward-filled on gaps); V_t = Σ scheme values
    4. F_t = Σ signed tx amounts since the previous grid day
    5. daily TWR r_t = (V_t - V_{t-1} - F_t) / V_{t-1}; total = Π(1+r)-1
    6. annualised once, till date (365.25 exponent) — ONLY when the observed
       span is >= MIN_CAGR_WINDOW_DAYS; otherwise honest null + reason
    7. XIRR from dated flows [-opening@start, ±tx amounts, +terminal@end]
    """
    if not transactions:
        return None
    parsed: list[dict] = []
    switches_skipped = 0
    for t in transactions:
        d = parse_nav_date(t.get("date") or "")
        # [perf-v1.4] SWITCH_IN/SWITCH_OUT are INTERNAL transfers, not cash:
        # excluded everywhere (units, flows, invested) per product decision.
        if (t.get("type") or "").upper() in ("SWITCH_IN", "SWITCH_OUT"):
            if d is not None:
                switches_skipped += 1
            continue
        units = float(t.get("cum_units") or 0.0)
        amt = float(t.get("amount") or 0.0)  # parse already signs by type
        if d is None or (units == 0.0 and amt == 0.0):
            continue
        code, isin = _tx_key(t)
        parsed.append({"amfi_code": code, "isin": isin, "d": d,
                       "units": units, "amt": amt,
                       "name": t.get("name") or ""})
    if not parsed:
        return None
    parsed.sort(key=lambda t: t["d"])

    # end units per scheme key (from the statement's current holdings)
    end_units: dict[tuple[str, str], float] = {}
    for it in items or []:
        u = it.get("units")
        try:
            u = float(u)
        except (TypeError, ValueError):
            continue
        if u > 0:
            key = ((it.get("amfi_code") or "").strip(),
                   (it.get("isin") or "").strip().upper())
            end_units[key] = end_units.get(key, 0.0) + u

    by_key: dict[tuple[str, str], list[dict]] = {}
    for t in parsed:
        by_key.setdefault((t["amfi_code"], t["isin"]), []).append(t)

    schemes: list[dict] = []
    for key, txs in by_key.items():
        nav_map = nav_lookup(key[0], key[1]) if nav_lookup else None
        if not nav_map:
            continue  # honest skip — no history for this scheme
        # normalise nav keys to date objects (lookup may return ISO strings)
        normalised: dict[date, float] = {}
        for k, v in nav_map.items():
            d = parse_nav_date(k)
            if d:
                normalised[d] = v
        if not normalised:
            continue
        signed_units = sum(t["units"] for t in txs)
        opening = end_units.get(key, 0.0) - signed_units
        if opening < 0:  # CAMS rounding/partial statements — never negative
            opening = 0.0
        schemes.append({"key": key, "txs": txs, "nav": normalised,
                        "end_units": end_units.get(key, 0.0),
                        "opening_units": opening,
                        "name": (txs[0].get("name") or "Scheme")})
    if not schemes:
        return {"error": "no NAV history for any scheme with transactions"}

    # [phantom-flow fix] Schemes without NAV history are honestly excluded
    # from valuation — their CASH must be excluded from the flow series too,
    # or every purchase/redemption in them becomes a phantom flow (value
    # unchanged, flow counted) that corrupts the daily TWR and the drawdown.
    # The excluded cash is reported in data_note, never silently dropped.
    valued_keys = {s["key"] for s in schemes}
    excluded_tx = [t for t in parsed if (t["amfi_code"], t["isin"]) not in valued_keys]
    parsed = [t for t in parsed if (t["amfi_code"], t["isin"]) in valued_keys]
    if not parsed:
        return {"error": "no NAV history for any scheme with transactions"}

    nav_dates = sorted({dd for s in schemes for dd in s["nav"].keys()})
    tx_dates = sorted({t["d"] for t in parsed})
    total_opening_units = sum(s["opening_units"] for s in schemes)
    # [perf-v1.4] series-start rule: a full-history statement (deduced opening
    # ~0) begins at the FIRST PURCHASE date — before that the account held
    # nothing; a partial statement (positive deduced opening) keeps the
    # earliest valuable NAV date and is flagged.
    partial_statement = total_opening_units >= 1.0
    if partial_statement:
        start = min(parsed[0]["d"], nav_dates[0]) if nav_dates else parsed[0]["d"]
    else:
        start = parsed[0]["d"]
    end = max([t["d"] for t in parsed] + (nav_dates or []))
    grid = sorted({d for d in nav_dates if start <= d <= end} | set(tx_dates))
    if len(grid) < 2:
        return {"error": "insufficient dated NAV/transaction points"}

    # per-scheme sorted nav keys for forward-fill via bisect
    for s in schemes:
        s["nav_seq"] = sorted(s["nav"].items())
        s["nav_dates"] = [dd for dd, _ in s["nav_seq"]]

    tx_by_date: dict[date, list[tuple[tuple[str, str], dict]]] = {}
    for t in parsed:
        tx_by_date.setdefault(t["d"], []).append(((t["amfi_code"], t["isin"]), t))

    values: dict[date, float] = {}
    flows: dict[date, float] = {}
    units_t: dict[tuple[str, str], float] = {}
    for d in grid:
        f = 0.0
        for k, t in tx_by_date.get(d, []):
            units_t[k] = units_t.get(k, 0.0) + t["units"]
            f += t["amt"]
        any_nav = False
        v = 0.0
        for s in schemes:
            i = bisect_left(s["nav_dates"], d)
            if i < len(s["nav_dates"]) and s["nav_dates"][i] == d:
                nav = s["nav_seq"][i][1]
            else:
                nav = s["nav_seq"][i - 1][1] if i > 0 else None
            if not nav or nav <= 0:
                continue
            any_nav = True
            u = units_t.get(s["key"], 0.0) + s["opening_units"]
            v += u * nav
        if not any_nav:
            continue  # nothing valued yet — no NAV data prior for any scheme
        values[d] = v  # zero-valued days KEPT: a full exit must show 0
        flows[d] = f

    if not any(values):
        return {"error": "insufficient dated value points"}
    # series spans the holding period: from the first value >= INR 1 (an
    # IEEE float-dust day like 2.6e-13 must NOT extend the window back to a
    # scheme's inception) through the later of the last material value / the
    # final transaction date
    non_zero = sorted(d for d in values if abs(values[d]) >= 1.0)
    lo = non_zero[0]
    hi = max(non_zero[-1], max(tx_dates))
    days = sorted(d for d in values if lo <= d <= hi)
    if len(days) < 2:
        return {"error": "insufficient dated value points"}

    vals = [values[d] for d in days]
    fl = [flows[d] for d in days]
    # daily TWR, end-of-day flow model: r_i = (V_i - F_i - V_{i-1}) / V_{i-1}
    # (F_i = amounts dated ON this grid day — they are already inside V_i).
    # A diversified mutual-fund portfolio in India has NEVER moved +/-20% in
    # one trading day (worst equity-fund single-day swings ~8%); larger daily
    # moves can only be statement-consistency artifacts (amount/units
    # mismatch on a redemption, partial-statement drift). Such days are
    # EXCLUDED from the geometric chain and reported via data_note — the
    # drawdown is nulled if any exist, never a fabricated -100%.
    ARTIFACT_THRESHOLD = 0.20  # 20% portfolio-level daily move bound
    artifact_days = 0
    rets: list[float] = []
    ret_days: list[date] = []  # each retained return is dated ITS OWN day
    linked = [1.0]
    for i in range(1, len(days)):
        v_prev = vals[i - 1]
        if v_prev <= 0:
            continue
        r = (vals[i] - fl[i] - v_prev) / v_prev
        if abs(r) > ARTIFACT_THRESHOLD:
            artifact_days += 1
            continue
        rets.append(r)
        ret_days.append(days[i])
        linked.append(linked[-1] * (1.0 + r))
    if not rets:
        return {"error": "no usable return observations"}
    total_twr = 1.0
    for r in rets:
        total_twr *= (1.0 + r)
    total_twr -= 1.0
    span_days = (days[-1] - days[0]).days
    opening_value = sum(s["opening_units"] * _nav_at(s, days[0])
                        for s in schemes if _nav_at(s, days[0]))
    terminal_value = vals[-1]
    total_net_flow = sum(t["amt"] for t in parsed)
    cash_in = sum(t["amt"] for t in parsed if t["amt"] > 0)
    cash_out = sum(t["amt"] for t in parsed if t["amt"] < 0)

    out: dict = {
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "days": span_days,
        "opening_value": round(opening_value, 2),
        "terminal_value": round(terminal_value, 2),
        "total_net_flow": round(total_net_flow, 2),
        "cash_in": round(cash_in, 2),
        "cash_out": round(cash_out, 2),
        "total_twr_pct": round(total_twr * 100.0, 2),
        "daily_returns": {"dates": [d.isoformat() for d in ret_days],
                          "values": [round(r * 100.0, 4) for r in rets]},
        "value_series": {"dates": [d.isoformat() for d in days],
                         "values": [round(v, 2) for v in vals]},
        "data_note": {
            "days": len(days),
            "artifact_days": artifact_days,
            "artifacts": artifact_days > 0,
            "switches_skipped": switches_skipped,
            "partial_statement": partial_statement,
            "start_reason": ("first_purchase" if not partial_statement
                             else "earliest_nav_partial_statement"),
            "unvalued_schemes": len({(t["amfi_code"], t["isin"])
                                     for t in excluded_tx}),
            "unvalued_tx_count": len(excluded_tx),
            "unvalued_net_flow": round(sum(t["amt"] for t in excluded_tx), 2),
        },
        "recon": {
            "opening_value_at_start": round(opening_value, 2),
            "opening_units_total": round(total_opening_units, 4),
            "flows_sum": round(total_net_flow, 2),
            "terminal_value": round(terminal_value, 2),
        },
        "max_drawdown_pct": (None if artifact_days
                             else _pct_or_none(max_drawdown(linked))),
        "drawdown_unavailable": ({"reason": "flow_adjustment_artifacts",
                                  "artifact_days": artifact_days}
                                 if artifact_days else None),
        "constituents": [
            {"name": s["name"], "amfi_code": s["key"][0],
             "isin": s["key"][1], "tx_count": len(s["txs"]),
             "opening_units": round(s["opening_units"], 4),
             "end_units": round(s["end_units"], 4),
             "first_tx": s["txs"][0]["d"].isoformat(),
             "last_tx": s["txs"][-1]["d"].isoformat()}
            for s in schemes],
        "methodology_version": METHODOLOGY_VERSION,
        "disclaimer": "Past performance is not indicative of future returns.",
    }
    xirr = _xirr([(days[0], -opening_value)] +
                 [(t["d"], -t["amt"]) for t in parsed] +
                 [(days[-1], terminal_value)])
    if span_days >= MIN_CAGR_WINDOW_DAYS and 1.0 + total_twr > 0:
        out["annualized_twr_pct"] = round(
            ((1.0 + total_twr) ** (DAYS_PER_YEAR / span_days) - 1.0) * 100.0, 2)
        out["xirr_pct"] = round(xirr * 100.0, 2) if xirr is not None else None
        out["annualized_window"] = {"start": days[0].isoformat(),
                                    "end": days[-1].isoformat(),
                                    "days": span_days}
    else:
        out["annualized_twr_pct"] = None
        out["xirr_pct"] = None
        out["annualized_unavailable"] = {
            "reason": ("insufficient_history" if span_days < MIN_CAGR_WINDOW_DAYS
                       else "compound_base_non_positive"),
            "required_span_days": MIN_CAGR_WINDOW_DAYS,
            "window_start": days[0].isoformat(),
            "window_end": days[-1].isoformat(),
            "span_days": span_days,
        }
    return out


def _nav_at(scheme: dict, d: date) -> float | None:
    nav = scheme["nav"].get(d)
    if nav is not None:
        return nav
    prev = [n for dd, n in scheme["nav_seq"] if dd < d]
    return prev[-1] if prev else None
