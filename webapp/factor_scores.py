"""Factor-score platform [factor-v1.0.0] — value / quality / momentum /
low-volatility composites across the tracked stock universe.

Pure functions, no I/O. House rules apply:

- Honest nulls: a component without its inputs (short history, missing
  financials) is None and is EXCLUDED from composites rather than zeroed;
  a factor composite needs >= MIN_COMPONENTS available ranks.
- Cross-sectional scores are percentile ranks 0-100 (higher = more of the
  factor). Raw z-scores are winsorized at +/-3 before ranking so a single
  outlier cannot flatten the whole cross-section.
- Sector-neutral mode ranks within each sector bucket instead of globally.

Data conventions:
- Momentum uses the classic skip-month layout: 12-1 = return from ~21
  trading days ago back to ~252 trading days ago (most recent month
  excluded to avoid short-term reversal contamination).
- Low volatility uses trailing 252-bar statistics annualised with the
  platform TRADING_DAYS constant; beta is OLS vs the supplied benchmark
  close series on common dates.
- Value/quality consume a fundamentals dict produced by the statement
  pipeline (webapp.stock_fundamental); until that lands for a stock the
  factors honestly report null.

Descriptive rankings only — no advice, no backtests, no performance
claims. Callers must render the standard disclaimer.
"""
from __future__ import annotations

import math

from .conventions import TRADING_DAYS

FACTOR_VERSION = "factor-v1.0.0"

MIN_BARS = 180            # universe membership gate (trailing bars)
MIN_COMPONENTS = 3        # components required for a factor composite
Z_CLIP = 3.0

MOMENTUM_COMPONENTS = ("ret_12_1", "ret_6_1", "ret_3_1", "ret_12_1_risk_adj")
LOWVOL_COMPONENTS = ("vol_252", "downside_dev", "beta", "max_drawdown")
VALUE_COMPONENTS = ("earnings_yield", "book_yield", "sales_yield",
                    "cashflow_yield", "ev_ebitda_inv", "dividend_yield")
QUALITY_COMPONENTS = ("roe_level", "roe_stability", "accruals_inv",
                      "leverage_inv", "margin_slope", "piotroski_scaled")

# components where LOW raw values are GOOD (inverted before ranking)
INVERTED_COMPONENTS = frozenset((
    "vol_252", "downside_dev", "beta",          # low-vol: lower is stronger
    "max_drawdown",                              # shallower drawdown stronger
    "accruals",                                  # quality internals
    "leverage",                                  # quality internals
    "ev_ebitda",                                 # value internals
))


# ---- per-stock raw metrics -----------------------------------------------------

def _daily_returns(closes: list[float]) -> list[float]:
    return [b / a - 1.0 for a, b in zip(closes, closes[1:])
            if a > 0 and b > 0]


def _std(returns: list[float]) -> float | None:
    if len(returns) < 30:
        return None
    m = sum(returns) / len(returns)
    var = sum((r - m) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var)


def _window_return(closes: list[float], lookback: int, skip: int = 0) -> float | None:
    """Total return over `lookback` bars ending `skip` bars before today."""
    end_i = len(closes) - 1 - skip
    start_i = end_i - lookback
    if start_i < 0 or end_i < 0 or len(closes) <= end_i:
        return None
    base, last = closes[start_i], closes[end_i]
    if base <= 0 or last <= 0:
        return None
    return last / base - 1.0


def _ols_beta(stock_rets: list[float], bench_rets: list[float]
              ) -> float | None:
    """OLS slope of stock returns on benchmark returns (paired by date)."""
    n = min(len(stock_rets), len(bench_rets))
    if n < 60:
        return None
    xs, ys = bench_rets[-n:], stock_rets[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sxx = 0.0
    for x, y in zip(xs, ys):
        cov += (x - mx) * (y - my)
        sxx += (x - mx) ** 2
    return cov / sxx if sxx else None


def _max_drawdown(closes: list[float]) -> float | None:
    peak = -math.inf
    worst = 0.0
    for v in closes:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst if math.isfinite(peak) else None


def momentum_metrics(closes: list[float | None]) -> dict:
    vals = [v for v in closes if v is not None]
    out = {k: None for k in MOMENTUM_COMPONENTS}
    if len(vals) < MIN_BARS + 21:
        return out
    out["ret_12_1"] = _window_return(vals, 252 - 21, skip=21)
    out["ret_6_1"] = _window_return(vals, 126 - 21, skip=21)
    out["ret_3_1"] = _window_return(vals, 63 - 21, skip=21)
    r121 = out["ret_12_1"]
    tail = vals[-252:]
    sigma = _std(_daily_returns(tail))
    if r121 is not None and sigma:
        out["ret_12_1_risk_adj"] = r121 / (sigma * math.sqrt(TRADING_DAYS))
    return out


def lowvol_metrics(closes: list[float | None],
                   bench_closes: list[tuple[str, float]] | None = None,
                   stock_dates: list[str] | None = None) -> dict:
    vals = [v for v in closes if v is not None][-252:]
    out = {k: None for k in LOWVOL_COMPONENTS}
    if len(vals) < MIN_BARS:
        return out
    rets = _daily_returns(vals)
    sigma = _std(rets)
    if sigma is not None:
        out["vol_252"] = sigma * math.sqrt(TRADING_DAYS) * 100.0
    downs = [r for r in rets if r < 0]
    dsigma = _std(downs)
    if dsigma is not None and len(downs) >= 30:
        out["downside_dev"] = dsigma * math.sqrt(TRADING_DAYS) * 100.0
    out["max_drawdown"] = _max_drawdown(vals)
    if bench_closes and stock_dates:
        bmap = {d: v for d, v in bench_closes}
        pairs_s, pairs_b = [], []
        for d, c in zip(stock_dates[-253:], vals):
            bv = bmap.get(d)
            if bv is not None and c is not None:
                pairs_s.append(c)
                pairs_b.append(bv)
        sr, br = _daily_returns(pairs_s), _daily_returns(pairs_b)
        out["beta"] = _ols_beta(sr, br)
    return out


def value_metrics(price: float | None,
                  fundamentals: dict | None) -> dict:
    """fundamentals keys (all per-share unless noted, TTM unless noted):
    eps, bvps, sps, cfps, ebitda_total, net_debt_total, dps_ttm."""
    out = {k: None for k in VALUE_COMPONENTS}
    if not fundamentals or not price or price <= 0:
        return out
    eps = fundamentals.get("eps")
    if eps is not None and eps != 0:
        out["earnings_yield"] = eps / price
    bvps = fundamentals.get("bvps")
    if bvps is not None and bvps > 0:
        out["book_yield"] = bvps / price
    sps = fundamentals.get("sps")
    if sps is not None and sps > 0:
        out["sales_yield"] = sps / price
    cfps = fundamentals.get("cfps")
    if cfps is not None and cfps > 0:
        out["cashflow_yield"] = cfps / price
    ebitda = fundamentals.get("ebitda_total")
    if ebitda is not None and ebitda > 0:
        nd = fundamentals.get("net_debt_total") or 0.0
        ev = price + nd  # per-share basis: caller passes share-normalised debt
        if ev > 0:
            out["ev_ebitda"] = ev / ebitda
    dps = fundamentals.get("dps_ttm")
    if dps is not None and dps > 0:
        out["dividend_yield"] = dps / price
    if out.get("ev_ebitda") is not None:
        out["ev_ebitda_inv"] = 1.0 / out.pop("ev_ebitda")
    else:
        out.pop("ev_ebitda", None)
    return out


def quality_metrics(fundamentals: dict | None) -> dict:
    """fundamentals keys: roe_series (list, oldest->newest),
    accruals ((NI-CFO)/TA latest), leverage (D/E latest),
    margin_series (list), piotroski (0-9)."""
    out = {k: None for k in QUALITY_COMPONENTS}
    if not fundamentals:
        return out
    roe_series = fundamentals.get("roe_series") or []
    roe_ok = [r for r in roe_series if r is not None]
    if roe_ok:
        out["roe_level"] = roe_ok[-1]
        if len(roe_ok) >= 3:
            m = sum(roe_ok) / len(roe_ok)
            sd = math.sqrt(sum((r - m) ** 2 for r in roe_ok)
                           / (len(roe_ok) - 1))
            if m != 0:
                out["roe_stability"] = -sd / abs(m)  # less noisy = higher
    acc = fundamentals.get("accruals")
    if acc is not None:
        out["accruals"] = -acc          # lower accruals are better quality
    lev = fundamentals.get("leverage")
    if lev is not None and lev >= 0:
        out["leverage"] = -lev
    margins = [m for m in (fundamentals.get("margin_series") or [])
               if m is not None]
    if len(margins) >= 3:
        n = len(margins)
        xs = list(range(n))
        mx, my = sum(xs) / n, sum(margins) / n
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, margins))
        sxx = sum((x - mx) ** 2 for x in xs)
        out["margin_slope"] = sxy / sxx if sxx else None
    f = fundamentals.get("piotroski")
    if f is not None:
        out["piotroski_scaled"] = f / 9.0
    # rename inverted internals into their ranked names
    for src, dst in (("accruals", "accruals_inv"),
                     ("leverage", "leverage_inv")):
        if out.get(src) is not None:
            out[dst] = out.pop(src)
        else:
            out.pop(src, None)
    return out


# ---- cross-sectional scoring ---------------------------------------------------

def _winsorized_z(values: list[float]) -> list[float]:
    n = len(values)
    if n < 2:
        return [0.0] * n
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 1.0
    return [max(-Z_CLIP, min(Z_CLIP, (v - m) / sd)) for v in values]


def _percentile_rank(z: float, zs: list[float]) -> float:
    """strictly-below count over (n - 1): a tie-free cross-section spans
    the full 0..100 range; tied values share the same rank."""
    below = sum(1 for x in zs if x < z)
    denom = len(zs) - 1
    if denom <= 0:
        return 50.0
    return 100.0 * below / denom


def rank_component(raw_by_isin: dict[str, float | None],
                   component: str = "") -> tuple[dict[str, float], int]:
    """Percentile ranks 0-100 (100 = strongest of the factor) for one raw
    component map keyed by ISIN. ``component`` selects the inversion rule
    (e.g. volatility: lower raw value -> higher rank). Returns
    ({isin: rank}, n_scored)."""
    flip = component in INVERTED_COMPONENTS
    usable = {k: (-v if flip else v)
              for k, v in raw_by_isin.items() if v is not None
              and isinstance(v, (int, float))
              and not (math.isnan(v) or math.isinf(v))}
    scored = dict(usable)
    if len(scored) < 5:
        return {}, len(scored)   # too thin a cross-section to rank honestly
    zs = _winsorized_z(list(scored.values()))
    zmap = dict(zip(scored.keys(), zs))
    ranks = {k: round(_percentile_rank(zmap[k], zs), 1)
             for k in scored}
    return ranks, len(scored)


def composite_from_ranks(ranks_by_component: dict[str, dict[str, float]],
                         min_components: int = MIN_COMPONENTS
                         ) -> dict[str, float]:
    """Mean of available component ranks per ISIN; needs >= min_components."""
    acc: dict[str, list[float]] = {}
    for comp_ranks in ranks_by_component.values():
        for isin, r in comp_ranks.items():
            acc.setdefault(isin, []).append(r)
    return {isin: round(sum(rs) / len(rs), 1) for isin, rs in acc.items()
            if len(rs) >= min_components}


def compute_factor_scores(raw: dict[str, dict], sectors: dict[str, str] | None = None
                          ) -> dict:
    """Cross-sectional factor scores for the universe.

    raw = {isin: {"momentum": {...}, "lowvol": {...},
                  "value": {...}, "quality": {...}}}
    Returns payload with per-component ranks, factor composites, a
    multi-factor composite, and coverage counts.
    """
    component_groups = {
        "momentum": MOMENTUM_COMPONENTS,
        "lowvol": LOWVOL_COMPONENTS,
        "value": VALUE_COMPONENTS,
        "quality": QUALITY_COMPONENTS,
    }
    ranks: dict[str, dict[str, dict[str, float]]] = {}
    coverage: dict[str, dict] = {}
    for group, comps in component_groups.items():
        ranks[group] = {}
        cov_n = {}
        for comp in comps:
            raw_map = {isin: rec.get(group, {}).get(comp)
                       for isin, rec in raw.items()}
            comp_ranks, n = rank_component(raw_map, component=comp)
            ranks[group][comp] = comp_ranks
            cov_n[comp] = n
        coverage[group] = {
            "components": cov_n,
            "composite_n": len(composite_from_ranks(
                {c: ranks[group][c] for c in comps})),
        }

    factor_scores: dict[str, dict[str, float]] = {}
    for group, comps in component_groups.items():
        comp_map = {c: ranks[group][c] for c in comps}
        for isin, score in composite_from_ranks(comp_map).items():
            factor_scores.setdefault(isin, {})[group] = score

    multi_acc: dict[str, list[float]] = {}
    for isin, fs in factor_scores.items():
        for v in fs.values():
            multi_acc.setdefault(isin, []).append(v)
    multi = {isin: round(sum(vs) / len(vs), 1)
             for isin, vs in multi_acc.items()
             if len(vs) >= 2}

    sector_ranks: dict[str, dict[str, float]] | None = None
    if sectors:
        sector_ranks = {}
        by_sector: dict[str, list[str]] = {}
        for isin in factor_scores:
            by_sector.setdefault(sectors.get(isin) or "__none__",
                                 []).append(isin)
        for group in ("momentum", "lowvol"):
            sec_scores: dict[str, float] = {}
            for _sec, isins in by_sector.items():
                if len(isins) < 5:
                    continue
                sub = {i: factor_scores[i].get(group)
                       for i in isins if factor_scores[i].get(group) is not None}
                if len(sub) < 5:
                    continue
                zs = _winsorized_z(list(sub.values()))
                zmap = dict(zip(sub.keys(), zs))
                for k, z in zmap.items():
                    sec_scores[k] = round(_percentile_rank(z, zs), 1)
            if sec_scores:
                for isin, sc in sec_scores.items():
                    sector_ranks.setdefault(isin, {})[group] = sc

    return {
        "methodology_version": FACTOR_VERSION,
        "universe_n": len(raw),
        "coverage": coverage,
        "component_ranks": ranks,
        "factor_scores": factor_scores,
        "multi_factor": multi,
        "sector_relative": sector_ranks,
    }


def screen(factor_scores: dict, *, factor: str = "multi", top_n: int = 20,
           ascending: bool = False, min_score_count: int = 2) -> list[dict]:
    """Ranked rows for the screener API. factor in
    value/quality/momentum/lowvol/multi."""
    if factor == "multi":
        source = factor_scores.get("multi_factor") or {}
    else:
        source = {isin: scores.get(factor)
                  for isin, scores in
                  (factor_scores.get("factor_scores") or {}).items()}
        source = {k: v for k, v in source.items() if v is not None}
    rows = [{"isin": isin, "score": score} for isin, score in source.items()]
    rows.sort(key=lambda r: r["score"], reverse=not ascending)
    return rows[:max(1, min(int(top_n), 200))]
