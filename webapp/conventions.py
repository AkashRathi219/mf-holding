"""Platform-wide analytic conventions [perf-v2.0.0].

ONE home for every constant, year-basis and unit rule the analytics stack
uses. Both apps (webapp + vendored copy in chatapp/app) import from here so
a number can never be annualised two different ways again.

Contents:
- Annualisation / gate constants (single definitions; analytics.py re-exports).
- METRIC_BANDS + normalize_metric(): the stored-unit guardrail. Universe /
  schemes-table metrics are FRACTIONS (TER 0.0072 = 0.72%); percent-scale
  sources leaking in (1.09 for 1.09%) are detected by plausibility bands and
  rescaled exactly once, at the boundary where data enters.
"""
from __future__ import annotations

try:                      # both apps ship an identical log.py interface
    from .log import get_logger
    _log = get_logger("conventions")
except Exception:         # pragma: no cover - fallback for odd embeds
    import logging
    _log = logging.getLogger("conventions")

# ---- annualisation / gates -----------------------------------------------------
TRADING_DAYS = 252             # trading-day annualisation factor (vol, TE, alpha)
DAYS_PER_YEAR = 365.25         # calendar-day basis (CAGR, XIRR, tenors) — THE
                               # only year length; never write 365 / 365.0 / 360
DEFAULT_RF_PCT = 6.0           # documented risk-free assumption (% p.a.)
ROLLING_WINDOW_YEARS = 1       # rolling-return window
ROLLING_WINDOW_DAYS = round(ROLLING_WINDOW_YEARS * DAYS_PER_YEAR)   # 365
ROLLING_GAP_SLACK_DAYS = 10    # single slack: a rolling base qualifies when it
                               # is >= window - slack calendar days back (both
                               # KPI cards and charts derive from ONE core)
MIN_POINTS_FOR_STATS = 30      # below this, risk metrics are None
MIN_CAGR_WINDOW_DAYS = 90      # below this even SI CAGR stays None
MIN_RISK_WINDOW_DAYS = 365     # risk stats need >=1y observed span

METHODOLOGY_VERSION = "perf-v2.0.0-2026-08-25"

# ---- stored-unit guardrail -----------------------------------------------------
# Bands are in STORED units (fractions for rate-like scheme metrics, years for
# tenor-like ones, percent for percent-of-NAV style ones).
METRIC_BANDS: dict[str, tuple[float, float]] = {
    "ter": (0.0, 0.05),            # >5% expense ratio does not exist in India
    "ytm": (0.0, 0.35),            # scheme-level yield-to-maturity (fraction)
    "duration": (0.0, 60.0),       # modified/macaulay duration, years
    "avg_maturity": (0.0, 60.0),   # average maturity, years
}


def normalize_metric(value, kind: str, *, source: str = ""):
    """Coerce ``value`` into the plausible stored-unit band for ``kind``.

    Returns the (possibly rescaled) float, or None when unusable. A value
    above the band that lands INSIDE it after /100 is treated as percent-
    scale contamination (the AMFI/universe dual-scale trap) and rescaled
    once, loudly logged. Values outside the band either way pass through
    unchanged - callers/data-health decide what to reject.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    lo, hi = METRIC_BANDS.get(kind, (float("-inf"), float("inf")))
    if lo <= v <= hi:
        return v
    scaled = v / 100.0
    if v > hi and lo <= scaled <= hi:
        _log.warning(
            "metric %s=%s from %s is percent-scale in a fraction column; "
            "rescaled to %s", kind, v, source or "?", round(scaled, 8))
        return scaled
    _log.warning("metric %s=%s from %s outside plausible band [%s, %s]",
                 kind, v, source or "?", lo, hi)
    return v
