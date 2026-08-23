"""Data health score (superadmin): composite 0-100 + component breakdown.

Components (weights):
- coverage      25  share of schemes with holdings (discovery_needed half-
                    credit; no-disclosure funds excluded from denominator)
- completeness  15  holdings ISIN completeness
- nav           20  share of nav_history files whose latest value is <= 10d old
- holdings      15  portfolio-disclosure freshness (as_of <= 45d; stale
                    AMC-disclosure-archive snapshots tracked separately,
                    feeds open item MF-A2)
- stocks_bonds  10   stale stock price files + bond catalog YTM/price coverage
- pipelines     15  refresh telemetry vs expected cadence (refresh_log)

A fresh computation appends a snapshot to data/logs/data_health.jsonl and
pushes it to R2 (best-effort) so history survives redeploys.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HISTORY_PATH = DATA_DIR / "logs" / "data_health.jsonl"

WEIGHTS = {"coverage": 25, "completeness": 15, "nav": 20,
           "holdings": 15, "stocks_bonds": 10, "pipelines": 15}

# Pipeline -> max hours since last successful run before it loses points.
# nav_daily runs twice a day (12h apart), so a 12h window keeps it green;
# stock/bond are once-daily (48h grace); amfi is monthly (35d).
CADENCE_HOURS = {"nav_daily": 12, "stock_refresh": 48,
                 "bond_refresh": 48, "amfi_fetch": 35 * 24}

MAX_AGE_DAYS = 10          # NAV / stock price freshness bar
HOLDINGS_STALE_DAYS = 45   # monthly disclosure cycle + slack
ADVISORKHOJ_STALE_DAYS = 180  # MF-A2 flag threshold

_lock = threading.Lock()
_cache: dict | None = None
_cached_at = 0.0


def invalidate() -> None:
    global _cache, _cached_at
    with _lock:
        _cache, _cached_at = None, 0.0


def band(score) -> str:
    if score is None:
        return "grey"
    return "green" if score >= 90 else ("amber" if score >= 70 else "red")


# ---- component scanners ----------------------------------------------------

def _coverage(meta: dict) -> tuple[float, dict]:
    dist = meta.get("coverage_dist") or {}
    total = sum(dist.values())
    has = dist.get("has_holdings", 0)
    discovery = dist.get("discovery_needed", 0)
    no_disc = dist.get("no_disclosure", 0)
    scoreable = total - no_disc
    if scoreable <= 0:
        return 0.0, {"total_schemes": total, **dist}
    credit = has + 0.5 * discovery
    return round(credit / scoreable * 100, 1), {
        "total_schemes": total, "has_holdings": has,
        "discovery_needed": discovery, "no_disclosure": no_disc,
        "missing": dist.get("missing", 0),
        "scoreable_excl_no_disclosure": scoreable,
    }


def _completeness(meta: dict) -> tuple[float, dict]:
    pct = meta.get("isin_completeness")
    return (float(pct) if pct is not None else 0.0), {
        "holdings": meta.get("holdings"),
        "holdings_with_isin": meta.get("holdings_with_isin"),
        "isin_completeness_pct": pct,
    }


def _file_freshness(dirpath: Path) -> tuple[int, int]:
    """(fresh_files, total_files) by latest-entry date <= MAX_AGE_DAYS."""
    from src.nav_freshness import stale_days
    fresh = total = 0
    if not dirpath.is_dir():
        return 0, 0
    for fn in dirpath.glob("*.json"):
        total += 1
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
            hist = doc.get("history") or doc.get("prices") or doc.get("data") or []
            last = hist[-1].get("date") if hist else None
        except Exception:
            continue
        days = stale_days(str(last)) if last else None
        if days is not None and days <= MAX_AGE_DAYS:
            fresh += 1
    return fresh, total


def _nav() -> tuple[float, dict]:
    fresh, total = _file_freshness(DATA_DIR / "nav_history")
    return (round(fresh / total * 100, 1) if total else 0.0), {
        "files": total, f"fresh_le_{MAX_AGE_DAYS}d": fresh}


def _holdings(wdb) -> tuple[float, dict]:
    rows = wdb.con.execute(
        "SELECT as_of, source FROM schemes WHERE coverage='has_holdings' "
        "AND as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchall()
    today = date.today()
    ages: list[int] = []
    ak_stale = ak_total = 0
    for r in rows:
        try:
            ages.append((today - date.fromisoformat(r["as_of"])).days)
        except Exception:
            continue
        if (r["source"] or "") == "advisorkhoj":
            ak_total += 1
            if ages[-1] > ADVISORKHOJ_STALE_DAYS:
                ak_stale += 1
    if not ages:
        return 0.0, {"dated_schemes": 0}
    within = sum(1 for a in ages if a <= HOLDINGS_STALE_DAYS)
    return round(within / len(ages) * 100, 1), {
        "dated_schemes": len(ages),
        "median_age_days": round(statistics.median(ages), 1),
        "max_age_days": max(ages),
        f"within_{HOLDINGS_STALE_DAYS}d": within,
        # Internal source key; surfaced as "AMC disclosure (archive)" upstream.
        "amc_disclosure_archive_total": ak_total,
        f"amc_disclosure_archive_stale_gt_{ADVISORKHOJ_STALE_DAYS}d": ak_stale,
    }


def _stocks_bonds(wdb) -> tuple[float, dict]:
    s_fresh, s_total = _file_freshness(DATA_DIR / "stock_history")
    s_score = (s_fresh / s_total * 100) if s_total else None
    cat = wdb._bond_catalog() or {}
    bonds = cat.get("bonds") or []
    n = len(bonds)
    ytm = sum(1 for b in bonds if b.get("ytm") is not None)
    price = sum(1 for b in bonds if b.get("price"))
    b_score = ((ytm + price) / (2 * n) * 100) if n else None
    parts = [x for x in (s_score, b_score) if x is not None]
    score = round(sum(parts) / len(parts), 1) if parts else 0.0
    return score, {
        "stock_files": s_total, "stock_fresh": s_fresh,
        "stock_pct": round(s_score, 1) if s_score is not None else None,
        "bonds": n, "bonds_with_ytm": ytm, "bonds_with_price": price,
        "bond_pct": round(b_score, 1) if b_score is not None else None,
    }


def _pipelines() -> tuple[float, dict]:
    from src.refresh_log import parse_ts, summary
    pipes = summary()["pipelines"]
    now = datetime.now(timezone.utc)
    detail = {}
    scores = []
    for name, max_h in CADENCE_HOURS.items():
        p = pipes.get(name) or {}
        last = parse_ts(p.get("last_ts")) if p.get("last_ts") else None
        age_h = round((now - last).total_seconds() / 3600, 1) if last else None
        status = p.get("last_status")
        if not last or not status:
            pts = 0.0
        elif status == "success":
            pts = 100.0 if age_h <= max_h else (
                50.0 if age_h <= 2 * max_h else 20.0)
        else:  # error
            pts = 25.0 if age_h <= max_h else 0.0
        scores.append(pts)
        detail[name] = {"status": status, "age_hours": age_h,
                        "cadence_hours": max_h, "points": pts,
                        "ok_24h": p.get("ok_24h"), "err_24h": p.get("err_24h")}
    return round(sum(scores) / len(scores), 1) if scores else 0.0, detail


# ---- public API -------------------------------------------------------------

def compute(force: bool = False, max_age_s: float = 300) -> dict:
    """Composite health snapshot (TTL-cached unless force=True)."""
    global _cache, _cached_at
    with _lock:
        if not force and _cache is not None \
                and time.time() - _cached_at < max_age_s:
            return _cache

    from .db import WebDB
    wdb = WebDB()
    meta = wdb.meta_stats()

    comps: dict[str, dict] = {}
    score, det = _coverage(meta)
    comps["coverage"] = {"score": score, "weight": WEIGHTS["coverage"], "detail": det}
    score, det = _completeness(meta)
    comps["completeness"] = {"score": score, "weight": WEIGHTS["completeness"], "detail": det}
    score, det = _nav()
    comps["nav"] = {"score": score, "weight": WEIGHTS["nav"], "detail": det}
    score, det = _holdings(wdb)
    comps["holdings"] = {"score": score, "weight": WEIGHTS["holdings"], "detail": det}
    score, det = _stocks_bonds(wdb)
    comps["stocks_bonds"] = {"score": score, "weight": WEIGHTS["stocks_bonds"], "detail": det}
    score, det = _pipelines()
    comps["pipelines"] = {"score": score, "weight": WEIGHTS["pipelines"], "detail": det}

    overall = round(
        sum(c["score"] * c["weight"] for c in comps.values()) / sum(WEIGHTS.values()), 1)
    from src.refresh_log import _now_ist
    out = {"overall": overall, "band": band(overall),
           "computed_at": _now_ist(),
           "components": comps}

    _append_history(out)
    with _lock:
        _cache, _cached_at = out, time.time()
    return out


def _append_history(snap: dict) -> None:
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": snap["computed_at"],
                           "overall": snap["overall"],
                           "components": {k: v["score"] for k, v in snap["components"].items()}},
                          ensure_ascii=False)
        with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return
    try:
        from .remote_store import is_configured, upload_object
        if is_configured():
            upload_object("logs/" + HISTORY_PATH.name, HISTORY_PATH)
    except Exception:
        pass


def read_history(limit: int = 200) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-max(1, limit):]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out[::-1]


# ---- per-scheme confidence badge + superadmin reliance metrics --------------

# Base trust by holdings source (mirrors _SOURCE_PRIORITY in webapp/db.py).
# 'advisorkhoj' is the internal key for the stable-link AMC-disclosure archive
# kept as background fallback; user-facing labels say "AMC disclosure".
SOURCE_BASE = {"amfi": 100, "amc_website": 88, "index": 85, "advisorkhoj": 62}
TIER_COLORS = {"green": "#3f9d63", "amber": "#d09a2f",
               "red": "#c94f4f", "grey": "#9aa0a6"}


def _as_of_age_days(as_of) -> int | None:
    try:
        return (date.today() - date.fromisoformat(str(as_of or "")[:10])).days
    except Exception:
        return None


def scheme_confidence(scheme: dict, stats: dict | None = None) -> dict:
    """0-100 data-confidence score for ONE scheme.

    source base - disclosure-age penalty, blended 70/30 with holdings quality
    (ISIN% / %NAV coverage) when stats are provided. Tiers: high >= 80,
    medium >= 55, low < 55; grey for schemes without any holdings record.

    Validation bonus: a FRESH (<=45d) snapshot that passes the merge-time
    weight checks (max <=100, sum <=120) with >=90% coverage on ISIN and %NAV
    earns +10 — provenance matters less than proven quality, so fully
    validated AMC-disclosure-archive data can reach High instead of being
    capped at Medium. Stale or unvalidated snapshots keep pure source-based
    scoring.
    """
    cov = scheme.get("coverage") or "has_holdings"
    src = scheme.get("source") or ""
    age = _as_of_age_days(scheme.get("as_of"))
    # [U3] explicit MF-A2 flag so any UI can badge staleness without
    # duplicating the threshold (>180d since last holdings disclosure).
    detail = {"source": src or None, "age_days": age,
              "stale": age is not None and age > ADVISORKHOJ_STALE_DAYS}

    if cov in ("no_disclosure", "missing"):
        return {"score": 0.0, "tier": "grey",
                "reason": cov, **detail}
    if cov == "discovery_needed":
        return {"score": 20.0, "tier": "red",
                "reason": "discovery_needed", **detail}

    score = float(SOURCE_BASE.get(src, 50))
    # Monthly disclosure cycle: nothing is stale before 45 days; 46-60d is a
    # mild concern; real decay starts past two missed cycles (>60d).
    if age is None:
        score -= 25          # undated disclosure
    elif age <= 45:
        pass                 # within the monthly cycle: full credit
    elif age <= 60:
        score -= 8           # a few days late - watch
    elif age <= 90:
        score -= 20          # second missed cycle
    elif age <= 180:
        score -= 35
    else:
        score -= 48          # MF-A2 stale territory

    n = (stats or {}).get("n") or 0
    if n:
        isin_pct = 100.0 * (stats.get("with_isin") or 0) / n
        pct_nav = 100.0 * (stats.get("with_pct") or 0) / n
        quality = round((isin_pct + pct_nav) / 2, 1)
        detail.update({"holdings": n, "isin_pct": round(isin_pct, 1),
                       "pct_nav_coverage": round(pct_nav, 1)})
        validated = False
        max_pct = stats.get("max_pct")
        sum_pct = stats.get("sum_pct")
        if max_pct is not None or sum_pct is not None:
            validated = ((max_pct is None or max_pct <= 100)
                         and (sum_pct is None or sum_pct <= 120))
        elif pct_nav > 0:
            validated = True  # weights present; no evidence of corruption
        if validated and isin_pct >= 90 and pct_nav >= 90 \
                and age is not None and age <= HOLDINGS_STALE_DAYS:
            score += 10.0     # validation bonus: fresh + proven usable data
            detail["validated"] = True
        score = round(score * 0.7 + quality * 0.3, 1)
    else:
        score = round(score, 1)

    tier = "green" if score >= 80 else ("amber" if score >= 55 else "red")
    return {"score": max(0.0, min(100.0, score)), "tier": tier, **detail}


def reliance_metrics(wdb, worst_limit: int = 15) -> dict:
    """Superadmin rollup: confidence distribution across ALL schemes."""
    rows = wdb.con.execute(
        """SELECT s.id, s.fund_name, s.amc, s.source, s.coverage, s.as_of,
                  COUNT(h.id) AS n,
                  SUM(CASE WHEN h.isin!='' THEN 1 ELSE 0 END) AS wi,
                  SUM(CASE WHEN h.percent_nav IS NOT NULL THEN 1 ELSE 0 END) AS wp,
                  MAX(h.percent_nav) AS max_pct,
                  SUM(h.percent_nav) AS sum_pct
           FROM schemes s LEFT JOIN holdings h ON h.scheme_id = s.id
           GROUP BY s.id""").fetchall()

    tiers = {"green": 0, "amber": 0, "red": 0, "grey": 0}
    src_stats: dict[str, dict] = {}
    scored: list[dict] = []
    total_score = 0.0
    scored_n = 0
    for r in rows:
        st = {"n": r["n"] or 0, "with_isin": r["wi"] or 0, "with_pct": r["wp"] or 0,
              "max_pct": r["max_pct"], "sum_pct": r["sum_pct"]}
        cf = scheme_confidence(dict(r), st)
        tiers[cf["tier"]] += 1
        src = r["source"] or "none"
        agg = src_stats.setdefault(src, {"count": 0, "score_sum": 0.0,
                                         "stale_gt_45d": 0,
                                         "isin_n": 0, "isin_sum": 0.0})
        agg["count"] += 1
        agg["score_sum"] += cf["score"]
        if cf.get("age_days") is not None and cf["age_days"] > HOLDINGS_STALE_DAYS:
            agg["stale_gt_45d"] += 1
        if cf.get("isin_pct") is not None:
            agg["isin_n"] += 1
            agg["isin_sum"] += cf["isin_pct"]
        if (r["coverage"] or "has_holdings") == "has_holdings":
            total_score += cf["score"]
            scored_n += 1
            scored.append({"id": r["id"], "fund_name": r["fund_name"],
                           "amc": r["amc"], "source": r["source"],
                           "as_of": r["as_of"], "age_days": cf.get("age_days"),
                           "score": cf["score"]})

    by_source = {}
    for src, a in src_stats.items():
        by_source[src] = {
            "count": a["count"],
            "avg_score": round(a["score_sum"] / a["count"], 1),
            "stale_gt_45d": a["stale_gt_45d"],
            "avg_isin_pct": round(a["isin_sum"] / a["isin_n"], 1) if a["isin_n"] else None,
        }

    scored.sort(key=lambda x: (x["score"], -(x["age_days"] or 0)))
    return {
        "total_schemes": len(rows),
        "tiers": tiers,
        "avg_score": round(total_score / scored_n, 1) if scored_n else None,
        "by_source": by_source,
        "worst": scored[:max(1, worst_limit)],
    }
