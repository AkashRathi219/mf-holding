"""Strategy rules: parse a plain-text rule box into structured rules and
evaluate a portfolio against them.

A rule text box example::

    Max 10% single stock. Max 20% sector. Min 30% debt.
    Max 5% cash. Max top-5 25%. Max scheme overlap 30%.

Unrecognised lines are kept as informational notes (not evaluated).
"""

from __future__ import annotations

import re

_ASSET_CANON = {
    "debt": "debt", "bond": "debt",
    "equity": "stocks", "stocks": "stocks", "stock": "stocks",
    "gold": "gold",
    "cash": "cash_equivalents", "cash equivalents": "cash_equivalents", "liquid": "cash_equivalents",
    "international": "international", "foreign": "international",
    "hybrid": "other",
}

_ASSET_NAMES = r"(?:debt|equity|stocks?|gold|cash(?:\s+equivalents)?|international|foreign|hybrid)"


def _asset_rule(m: re.Match) -> dict:
    op = (m.group(1) or "max").lower()
    if m.group("a1"):
        asset, value = m.group("a1"), m.group("v1")
    else:
        asset, value = m.group("a2"), m.group("v2")
    key = _ASSET_CANON.get(asset.lower().strip(), "other")
    return {"rule_type": "min_asset" if op == "min" else "max_asset",
            "field": key, "operator": ">=" if op == "min" else "<=",
            "value": float(value), "unit": "%", "severity": "high"}


_RULES = [
    (re.compile(
        r"max(?:imum)?\s+(?:(?:single\s+)?(?:stock|holding|company|scrip)\s*(?:weight|concentration)?\s*[=:]?\s*(?P<n1>\d+(?:\.\d+)?)\s*%"
        r"|(?P<n2>\d+(?:\.\d+)?)\s*%\s*(?:single\s+)?(?:stock|holding|company|scrip))", re.I),
     lambda m: {"rule_type": "max_single_stock", "field": "single stock", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "%", "severity": "high"}),
    (re.compile(
        r"max(?:imum)?\s+(?:sector\s*(?:exposure|weight|concentration)?\s*[=:]?\s*(?P<n1>\d+(?:\.\d+)?)\s*%"
        r"|(?P<n2>\d+(?:\.\d+)?)\s*%\s*sector)", re.I),
     lambda m: {"rule_type": "max_sector", "field": "sector", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "%", "severity": "high"}),
    (re.compile(r"(min|max)(?:imum)?\s+(?:(?P<a1>" + _ASSET_NAMES + r")\s*(?:allocation|exposure|weight)?\s*[=:]?\s*(?P<v1>\d+(?:\.\d+)?)\s*%"
                r"|(?P<v2>\d+(?:\.\d+)?)\s*%\s*(?P<a2>" + _ASSET_NAMES + r"))", re.I),
     _asset_rule),
    (re.compile(
        r"max(?:imum)?\s+(?:top[- ]?5\s*(?:concentration|weight|holdings)?\s*[=:]?\s*(?P<n1>\d+(?:\.\d+)?)\s*%"
        r"|(?P<n2>\d+(?:\.\d+)?)\s*%\s*top[- ]?5)", re.I),
     lambda m: {"rule_type": "max_top5", "field": "top 5 concentration", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "%", "severity": "medium"}),
    (re.compile(
        r"max(?:imum)?\s+(?:top[- ]?10\s*(?:concentration|weight|holdings)?\s*[=:]?\s*(?P<n1>\d+(?:\.\d+)?)\s*%"
        r"|(?P<n2>\d+(?:\.\d+)?)\s*%\s*top[- ]?10)", re.I),
     lambda m: {"rule_type": "max_top10", "field": "top 10 concentration", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "%", "severity": "medium"}),
    (re.compile(
        r"max(?:imum)?\s+(?:overlap(?: between schemes)?\s*[=:]?\s*(?P<n1>\d+(?:\.\d+)?)\s*%"
        r"|(?P<n2>\d+(?:\.\d+)?)\s*%\s*overlap)", re.I),
     lambda m: {"rule_type": "max_overlap", "field": "scheme overlap", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "%", "severity": "high"}),
    (re.compile(
        r"max(?:imum)?\s+(?:(?:number\s+of\s+)?schemes?\s*[=:]?\s*(?P<n1>\d+)|(?P<n2>\d+)\s*(?:number\s+of\s+)?schemes?)", re.I),
     lambda m: {"rule_type": "max_schemes", "field": "schemes", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "count", "severity": "low"}),
    (re.compile(
        r"min(?:imum)?\s+(?:(?:number\s+of\s+)?schemes?\s*[=:]?\s*(?P<n1>\d+)|(?P<n2>\d+)\s*(?:number\s+of\s+)?schemes?)", re.I),
     lambda m: {"rule_type": "min_schemes", "field": "schemes", "operator": ">=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "count", "severity": "low"}),
    (re.compile(
        r"min(?:imum)?\s+(?:holdings?\s*[=:]?\s*(?P<n1>\d+)|(?P<n2>\d+)\s*holdings?)", re.I),
     lambda m: {"rule_type": "min_holdings", "field": "holdings", "operator": ">=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "count", "severity": "medium"}),
    (re.compile(
        r"max(?:imum)?\s+(?:holdings?\s*[=:]?\s*(?P<n1>\d+)|(?P<n2>\d+)\s*holdings?)", re.I),
     lambda m: {"rule_type": "max_holdings", "field": "holdings", "operator": "<=",
                "value": float(m.group("n1") or m.group("n2")), "unit": "count", "severity": "medium"}),
]

# Cap-level (sub-asset-class) rules: large / mid / small / micro cap allocations.
_CAP_LABELS = (("large[- ]?cap", "large cap"), ("mid[- ]?cap", "mid cap"),
               ("small[- ]?cap", "small cap"), ("micro[- ]?cap", "microcap"))


def _cap_rule(prefix: str, label_re: str, key: str):
    rx = re.compile(
        rf"{prefix}(?:imum)?\s+(?:{label_re}\s*(?:allocation|exposure|weight|stocks?)?\s*[=:]?\s*(?P<n1>\d+(?:\.\d+)?)\s*%"
        rf"|(?P<n2>\d+(?:\.\d+)?)\s*%\s*{label_re})", re.I)
    return rx, lambda m: {"rule_type": f"{prefix}_{key.replace(' ', '_')}",
                          "field": key, "operator": ">=" if prefix == "min" else "<=",
                          "value": float(m.group("n1") or m.group("n2")),
                          "unit": "%", "severity": "medium"}


for _p in ("min", "max"):
    for _lbl, _key in _CAP_LABELS:
        _RULES.append(_cap_rule(_p, _lbl, _key))


def parse_rules(text: str) -> dict:
    """Parse rule text into structured rules + unparsed informational notes."""
    text = text or ""
    rules: list[dict] = []
    spans: list[tuple[int, int]] = []
    for rx, handler in _RULES:
        for m in rx.finditer(text):
            entry = handler(m)
            if entry:
                rules.append(entry)
                spans.append(m.span())
    spans.sort()
    unparsed: list[str] = []
    cursor = 0
    for s, e in spans:
        if s > cursor:
            chunk = text[cursor:s].strip(" \t.;,\n")
            if chunk:
                unparsed.append(chunk)
        cursor = max(cursor, e)
    tail = text[cursor:].strip(" \t.;,\n")
    if tail:
        unparsed.append(tail)
    for r in rules:
        r["remark"] = ""
    return {"rules": rules, "unparsed": unparsed}


def _asset_actual(metrics: dict, asset_key: str) -> float | None:
    """Actual % for an asset bucket. A bucket absent from the split is a
    genuine 0.0 only when the portfolio was fully resolved; on partial
    resolution the true weight is unknown -> None (rule reported N/A)."""
    split = metrics.get("asset_split") or {}
    if asset_key in split:
        v = split.get(asset_key)
        return None if v is None else float(v)
    if metrics.get("_portfolio_resolved"):
        return 0.0
    return None


def _actual_for(rule: dict, metrics: dict) -> float | None:
    rt = rule.get("rule_type")
    if rt == "max_single_stock":
        v = metrics.get("single_stock_max")
        return None if v is None else float(v)
    if rt == "max_sector":
        v = metrics.get("sector_max")
        return None if v is None else float(v)
    if rt in ("min_asset", "max_asset"):
        return _asset_actual(metrics, rule.get("field") or "other")
    if rt in ("min_large_cap", "max_large_cap", "min_mid_cap", "max_mid_cap",
              "min_small_cap", "max_small_cap", "min_microcap", "max_microcap"):
        v = (metrics.get("cap_split") or {}).get(rule.get("field") or "")
        return None if v is None else float(v)
    if rt == "max_top5":
        v = metrics.get("top5")
        return None if v is None else float(v)
    if rt == "max_top10":
        v = metrics.get("top10")
        return None if v is None else float(v)
    if rt == "max_overlap":
        v = metrics.get("overlap_max")
        return None if v is None else float(v)
    if rt == "max_schemes":
        v = metrics.get("n_schemes")
        return None if v is None else float(v)
    if rt == "min_schemes":
        v = metrics.get("n_schemes")
        return None if v is None else float(v)
    if rt == "min_holdings":
        v = metrics.get("n_holdings")
        return None if v is None else float(v)
    if rt == "max_holdings":
        v = metrics.get("n_holdings")
        return None if v is None else float(v)
    return None


def evaluate_rules(rules: list[dict], metrics: dict) -> dict:
    """Evaluate structured rules against portfolio metrics.

    ``metrics`` = {single_stock_max, sector_max, asset_split, cap_split, top5,
    top10, overlap_max, n_schemes, n_holdings}. A rule whose metric is unavailable
    (``None`` — e.g. security-level rules for an allocation-only model) is reported
    as N/A and excluded from the compliance score.
    """
    results = []
    passed = failed = 0
    for r in rules:
        actual = _actual_for(r, metrics)
        if actual is None:
            results.append({
                "rule": _label(r),
                "rule_type": r.get("rule_type"),
                "field": r.get("field"), "operator": r.get("operator"),
                "limit": _fmt_limit(r), "actual": "N/A",
                "pass": None, "severity": r.get("severity") or "low", "na": True,
                "remark": r.get("remark") or "",
            })
            continue
        if r.get("value") is None:
            # a persisted rule without a limit value cannot be scored
            results.append({
                "rule": _label(r),
                "rule_type": r.get("rule_type"),
                "field": r.get("field"), "operator": r.get("operator"),
                "limit": _fmt_limit(r), "actual": "N/A",
                "pass": None, "severity": r.get("severity") or "low", "na": True,
                "remark": r.get("remark") or "rule has no limit value",
            })
            continue
        limit = float(r.get("value"))
        op = r.get("operator") or "<="
        ok = actual <= limit if op == "<=" else actual >= limit
        unit = r.get("unit") or "%"
        if unit == "%":
            actual_s, limit_s = f"{actual:.1f}%", f"{limit:.1f}%"
        else:
            actual_s, limit_s = f"{int(round(actual))}", f"{int(round(limit))}"
        if ok:
            passed += 1
        else:
            failed += 1
        results.append({
            "rule": _label(r),
            "rule_type": r.get("rule_type"),
            "field": r.get("field"), "operator": op,
            "limit": limit_s, "actual": actual_s,
            "pass": ok, "severity": r.get("severity") or "low",
            "remark": r.get("remark") or "",
        })
    total = passed + failed
    # Zero evaluable rules is NOT full compliance — report None so the UI can
    # show an explicit N/A instead of a misleading green 100%.
    compliance = round(passed / total * 100, 1) if total else None
    return {"rows": results, "passed": passed, "failed": failed,
            "total": total, "na": len(results) - total, "compliance": compliance}


def _fmt_limit(r: dict) -> str:
    unit = r.get("unit") or "%"
    if unit == "%":
        return f"{r.get('value')}%"
    return f"{int(round(r.get('value') or 0))}"


def _label(r: dict) -> str:
    field = r.get("field") or ""
    op = "at most" if r.get("operator") == "<=" else "at least"
    unit = r.get("unit") or "%"
    rt = r.get("rule_type") or ""
    if rt in ("min_asset", "max_asset"):
        field_label = field.replace("_", " ").replace("cash equivalents", "cash").replace("stocks", "equity")
        return f"{field_label} {op} {r.get('value')}{unit}"
    if unit == "%":
        return f"{field} {op} {r.get('value')}%"
    return f"{field} {op} {int(round(r.get('value') or 0))}"