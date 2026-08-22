"""FastAPI application for the Factsheet Engine AI webapp.

Serves the static frontend and a JSON API over the built holdings database.
All data endpoints require a valid session token (login / register first).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db
from .remote_store import ensure as remote_ensure
from .tools_api import router as tools_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

SUPERADMIN_EMAILS = {e.strip().lower() for e in os.environ.get(
    "SUPERADMIN_EMAILS", "akash@aracharatventures.com").split(",") if e.strip()}

app = FastAPI(title="Factsheet Engine AI", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.include_router(tools_router)


# --------------------------------------------------------------------------
# Scheduled jobs (opt-in via ENABLE_SCHEDULER=1; set on Railway)
# --------------------------------------------------------------------------

_job_runner_started = False


def _amfi_job() -> dict:
    from src.refresh_log import track
    with track("amfi_fetch") as _meta:
        from webapp.amfi_fetch import fetch_mfdata, save
        data = fetch_mfdata()
        paths = save(data, "latest") if data else []
        _meta.update(amcs=len(data), files=len(paths))
        return {"amcs": len(data), "files": len(paths)}


def _bond_job() -> dict:
    from datetime import date as _date, timedelta as _td
    from src.refresh_log import track
    with track("bond_refresh") as _meta:
        try:
            from .remote_store import ensure_prefix
            _meta["cached_dumps_pulled"] = ensure_prefix("bond_market")
        except Exception:
            pass
        from src.bonds import build_catalog, fetch_day
        # Walk back over weekends/holidays until a day with published files.
        files: dict = {}
        d = _date.today()
        for _ in range(7):
            files = fetch_day(d)
            if files:
                break
            d -= _td(days=1)
        catalog = build_catalog()
        _meta.update(files=len(files), fetched_for=str(d),
                     bonds=catalog["n_bonds"], as_of=catalog["as_of"])
        return {"files": len(files), "fetched_for": str(d),
                "bonds": catalog["n_bonds"]}


def _nav_job() -> dict:
    """Daily NAV refresh + per-scheme gap-fill from last-known date to today."""
    from src.refresh_log import track
    with track("nav_daily", mode="scheduled") as meta:
        try:
            from .remote_store import ensure_prefix
            meta["universe_pulled"] = ensure_prefix("universe")
        except Exception:
            pass
        from src.nav_daily import (_update_latest_navs_impl,
                                   fill_gaps_from_last_known)
        s1 = _update_latest_navs_impl(days=7)  # impl: no nested telemetry event
        meta.update(s1)
        gaps = fill_gaps_from_last_known()
        meta.update({f"gap_{k}": v for k, v in gaps.items()})
        return {**s1, "gapfill": gaps}


def _noop_pipeline(*args, **kwargs) -> None:
    # Monthly AMC-site downloads stay out of the web container (heavy PDFs);
    # they run from a workstation via `python main.py run` / the CLI scheduler.
    from src.refresh_log import record
    record("monthly_holdings_fetch", "skipped",
           note="runs from workstation pipeline, not the web container")


def _start_scheduler_thread() -> None:
    """Run the cron jobs inside the web process when ENABLE_SCHEDULER=1."""
    global _job_runner_started
    if _job_runner_started or os.environ.get("ENABLE_SCHEDULER") != "1":
        return
    import asyncio

    import yaml

    from src.scheduler import MonthlyScheduler

    def _loop() -> None:
        async def runner() -> None:
            config = {}
            cfg_path = BASE_DIR.parent / "config" / "settings.yaml"
            try:
                config = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
            from src.nav_daily import update_latest_navs
            from src.stock_refresh import refresh_all
            sched = MonthlyScheduler(
                _noop_pipeline, config, base_dir=BASE_DIR.parent,
                nav_refresh_fn=_nav_job,
                stock_refresh_fn=refresh_all,
                bond_refresh_fn=_bond_job,
                amfi_fn=_amfi_job,
            )
            sched.start()
            while True:
                await asyncio.sleep(3600)

        asyncio.run(runner())

    threading.Thread(target=_loop, name="job-scheduler", daemon=True).start()
    _job_runner_started = True


@app.on_event("startup")
def _on_startup() -> None:
    _start_scheduler_thread()


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Never cache static assets so JS/CSS edits are picked up on the next load."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

_db_lock = threading.Lock()
_db: db.WebDB | None = None


def get_db() -> db.WebDB:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = db.get_db()
    return _db


# --------------------------------------------------------------------------
# Auth models
# --------------------------------------------------------------------------

class RegisterIn(BaseModel):
    name: str
    email: str
    org: str = ""
    password: str

class LoginIn(BaseModel):
    email: str
    password: str


def _require_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.headers.get("X-Auth-Token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required. Please log in.")
    user = auth.user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    return user


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def api_register(body: RegisterIn):
    try:
        return auth.register_user(body.email, body.name, body.org, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login")
def api_login(body: LoginIn):
    try:
        return auth.login_user(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/auth/me")
def api_me(request: Request):
    user = _require_user(request)
    return {"user": user,
            "superadmin": (user.get("email") or "").lower() in SUPERADMIN_EMAILS}


# --------------------------------------------------------------------------
# Data API (all require auth)
# --------------------------------------------------------------------------

def _page(request: Request) -> tuple[int, int]:
    try:
        limit = max(1, min(int(request.query_params.get("limit", 50)), 500))
    except ValueError:
        limit = 50
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError:
        offset = 0
    return limit, offset


@app.get("/api/meta")
def api_meta(request: Request):
    _require_user(request)
    return get_db().meta_stats()


@app.get("/api/schemes")
def api_schemes(request: Request):
    _require_user(request)
    p = request.query_params
    limit, offset = _page(request)
    data = get_db().list_schemes(
        amc=p.get("amc"), category=p.get("category"), source=p.get("source"),
        coverage=p.get("coverage"), search=p.get("search"), cap=p.get("cap"),
        sector=p.get("sector"),
        is_index=_opt_bool(p.get("is_index")), is_etf=_opt_bool(p.get("is_etf")),
        limit=limit, offset=offset)
    return data


@app.get("/api/cas-sample")
def api_cas_sample(request: Request):
    """The CAS sample portfolio (CAS_sample_portfolio_holdings.json) as builder
    items, weighted by current market value."""
    _require_user(request)
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "CAS_sample_portfolio_holdings.json"
    if not p.exists():
        remote_ensure("CAS_sample_portfolio_holdings.json",
                      dest=Path(__file__).resolve().parent.parent / "CAS_sample_portfolio_holdings.json")
    if not p.exists():
        raise HTTPException(status_code=404, detail="No CAS sample on record.")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="CAS sample unreadable.")
    items = []
    for a in (doc.get("portfolio_summary") or {}).get("allocations") or []:
        name = (a.get("scheme_name") or "").strip()
        isin = (a.get("isin") or "").strip().upper()
        if not name or not isin or a.get("net_units") in (None, 0, 0.0):
            continue
        weight = a.get("allocation_pct_market_value") or a.get("allocation_pct") or 0
        items.append({"type": "scheme", "isin": isin, "name": name,
                      "weight": round(float(weight), 2), "units": a.get("net_units")})
    return {"label": "CAS Sample", "source": "CAS_sample_portfolio_holdings.json", "items": items}


@app.get("/api/schemes/{scheme_id}")
def api_scheme_detail(scheme_id: int, request: Request, holdings: int = 0):
    _require_user(request)
    scheme = get_db().get_scheme(scheme_id)
    if not scheme:
        raise HTTPException(status_code=404, detail="Scheme not found.")
    out = dict(scheme)
    # Two important dates for every scheme:
    #   1. nav_date        — latest NAV value date (refreshed DAILY)
    #   2. as_of / holdings_date — portfolio-holdings announcement date (WEEKLY)
    nav_date, nav_value = _latest_nav(scheme)
    out["nav_date"] = nav_date
    out["nav_value"] = nav_value
    out["holdings_date"] = out.get("as_of") or None
    if holdings:
        out["holdings"] = get_db().scheme_holdings(scheme_id)
    return out


def _latest_nav(scheme: dict) -> tuple:
    """Freshest daily NAV (date, value) for a scheme: cross-checks the
    market-value ISIN index with the scheme's own nav-history file (by its AMFI
    code) so every scheme resolves its true latest daily NAV date."""
    try:
        from .market_value import _dtkey, latest_nav_index, scheme_latest_nav
        idx = latest_nav_index()
    except Exception:
        idx = {}
    best_date, best_value, best_key = None, None, (0, 0, 0)
    for isin in (scheme.get("isin_direct"), scheme.get("isin_regular")):
        rec = idx.get((isin or "").strip().upper()) if isin else None
        if rec and rec.get("date"):
            k = _dtkey(rec["date"])
            if k > best_key:
                best_date, best_value, best_key = rec["date"], rec.get("nav"), k
    try:
        got = scheme_latest_nav(scheme, prefer="direct")
        if got and got[1]:
            k = _dtkey(got[1])
            if k >= best_key:  # Direct plan wins date ties for the drawer
                best_date, best_value, best_key = got[1], got[0], k
    except Exception:
        pass
    return best_date, best_value


@app.get("/api/schemes/{scheme_id}/nav")
def api_scheme_nav(scheme_id: int, request: Request):
    _require_user(request)
    scheme = get_db().get_scheme(scheme_id)
    if not scheme:
        raise HTTPException(status_code=404, detail="Scheme not found.")
    p = request.query_params
    return get_db().scheme_nav(
        scheme_id, start=p.get("start") or None, end=p.get("end") or None)


@app.get("/api/securities")
def api_securities(request: Request):
    _require_user(request)
    p = request.query_params
    limit, offset = _page(request)
    ce = None
    if p.get("confirmed_equity") is not None:
        try:
            ce = float(p["confirmed_equity"])
        except ValueError:
            ce = None
    return get_db().list_securities(
        q=p.get("q"), confirmed_equity=ce, cap=p.get("cap"), sector=p.get("sector"),
        limit=limit, offset=offset)


@app.get("/api/securities/{isin}")
def api_security_detail(isin: str, request: Request):
    _require_user(request)
    sec = get_db().get_security(isin.upper())
    if not sec:
        raise HTTPException(status_code=404, detail="Security not found.")
    sec["used_in"] = get_db().security_usage(isin.upper(), limit=20)
    return sec


@app.get("/api/securities/{isin}/price")
def api_security_price(isin: str, request: Request):
    _require_user(request)
    if not get_db().get_security(isin.upper()):
        raise HTTPException(status_code=404, detail="Security not found.")
    p = request.query_params
    data = get_db().stock_price(isin.upper(), start=p.get("start") or None,
                                end=p.get("end") or None)
    return data or {"isin": isin.upper(), "available": False}


@app.get("/api/securities/{isin}/actions")
def api_security_actions(isin: str, request: Request):
    _require_user(request)
    if not get_db().get_security(isin.upper()):
        raise HTTPException(status_code=404, detail="Security not found.")
    data = get_db().stock_actions(isin.upper())
    return data or {"isin": isin.upper(), "available": False}


@app.get("/api/securities/{isin}/reports")
def api_security_reports(isin: str, request: Request):
    _require_user(request)
    if not get_db().get_security(isin.upper()):
        raise HTTPException(status_code=404, detail="Security not found.")
    data = get_db().stock_reports(isin.upper())
    return data or {"isin": isin.upper(), "available": False}


@app.post("/api/overlap")
def api_overlap(request: Request, body: dict):
    _require_user(request)
    ids = body.get("scheme_ids") or []
    items = body.get("items")
    wdb = get_db()
    if (not ids or len(ids) < 2) and items:
        ids = wdb.resolve_scheme_ids(items)
    if not ids or len(ids) < 2:
        raise HTTPException(status_code=400, detail="Select at least two schemes.")
    ids = [int(i) for i in ids]
    if len(ids) > 12:
        raise HTTPException(status_code=400, detail="Select at most 12 schemes.")
    return wdb.overlap(ids)


@app.post("/api/portfolio/analysis")
def api_portfolio_analysis(request: Request, body: dict):
    _require_user(request)
    items = body.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="Provide at least one portfolio item.")
    if len(items) > 50:
        raise HTTPException(status_code=400, detail="At most 50 items per analysis.")
    wdb = get_db()
    pa = wdb.portfolio_analysis(items)
    scheme_ids = wdb.resolve_scheme_ids(items)
    ov = wdb.overlap(scheme_ids) if len(scheme_ids) >= 2 else {"matrix": [], "concentration": []}
    pa["overlap"] = ov["matrix"]
    return pa


@app.post("/api/feedback")
def api_feedback(request: Request, body: dict):
    """Capture feature-feedback from the portfolio tools (appended to data/feedback.json)."""
    user = _require_user(request)
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Feedback message is required.")
    from datetime import datetime
    from pathlib import Path
    import json
    # repo-root data/ (not webapp/data/) so all app data stays under one tree
    path = BASE_DIR.parent / "data" / "feedback.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            records = []
    records.append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "user": (user.get("email") or user.get("name") or ""),
        "message": message,
        "context": body.get("context") or "",
    })
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok", "total": len(records)}


@app.post("/api/proposal")
def api_proposal(request: Request, body: dict):
    _require_user(request)
    items = body.get("items")
    if not items:
        ids = body.get("scheme_ids") or []
        if len(ids) < 1:
            raise HTTPException(status_code=400, detail="Provide portfolio items (or at least one scheme).")
        n = len(ids)
        items = [{"type": "scheme", "id": int(x), "weight": round(100.0 / n, 2)} for x in ids]
    if len(items) > 50:
        raise HTTPException(status_code=400, detail="At most 50 items per proposal.")
    return build_proposal(get_db(), items, body.get("replacements") or {},
                          body.get("remarks") or {})


def _md_cell(text) -> str:
    """Sanitize free text for a markdown table cell (pipe/newline safe)."""
    s = str(text or "").strip()
    s = s.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")
    return s or "—"


def build_proposal(wdb: db.WebDB, items: list[dict], replacements: dict,
                   remarks: dict | None = None) -> dict:
    """Generate the 3-section white-label proposal markdown over a weighted
    portfolio of schemes + direct stocks.

    ``items`` = [{type: scheme|stock, id|name|isin, weight: pct}],
    ``replacements``/``remarks`` = {item-index: text} (advisor-editable).
    """
    replacements = replacements or {}
    remarks = remarks or {}
    pa = wdb.portfolio_analysis(items)
    scheme_ids = wdb.resolve_scheme_ids(items)
    ov = wdb.overlap(scheme_ids) if len(scheme_ids) >= 2 else {"matrix": [], "concentration": []}

    lines: list[dict] = []
    for idx, item in enumerate(items):
        itype = (item.get("type") or "").lower()
        try:
            weight = float(item.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0.0
        if itype in ("scheme", "fund", "mf"):
            s = wdb._resolve_scheme_item(item)
            if not s:
                lines.append({"index": idx, "type": "scheme",
                              "name": item.get("name") or str(item.get("id") or ""),
                              "category": "—", "weight": weight,
                              "rationale": "Scheme not found.", "resolved": False})
                continue
            if s.get("n_holdings"):
                rationale = (f"Portfolio of {s['n_holdings']} holdings; top holding "
                             f"{s.get('top_holding', '')} at {s.get('top_holding_pct') or 0:.2f}%.")
            else:
                rationale = "Tracked index exposure with benchmark-aligned holdings."
            lines.append({"index": idx, "type": "scheme", "name": s["fund_name"],
                          "category": s.get("category") or "—", "weight": weight,
                          "rationale": rationale, "resolved": True})
        else:
            sec = wdb._resolve_stock_item(item)
            if not sec:
                lines.append({"index": idx, "type": "stock",
                              "name": item.get("name") or item.get("isin") or "",
                              "category": "—", "weight": weight,
                              "rationale": "Stock not found.", "resolved": False})
                continue
            lines.append({"index": idx, "type": "stock", "name": sec.get("name") or "",
                          "category": sec.get("sector") or "—", "weight": weight,
                          "rationale": "Direct equity holding.", "resolved": True})

    out: list[str] = []
    out.append("# Client Portfolio Proposal")
    out.append("")
    out.append(f"_Generated on {_today_str()} - Factsheet Engine AI_")
    out.append("")
    out.append(f"Portfolio: {len(pa['schemes'])} schemes · {len(pa['stocks'])} direct stocks · "
               f"allocated {pa['total_weight']:.1f}% · {pa['n_holdings']} underlying securities")
    out.append("")
    out.append("## 1. Current Portfolio Diagnostic")
    out.append("")
    pairs = _top_overlap_pairs(ov["matrix"], k=5) if ov["matrix"] else []
    high = [p for p in pairs if p[2] >= 60]
    if high:
        out.append("**High-overlap pairs (>=60% stock overlap):**")
        out.append("")
        out.append("| Scheme A | Scheme B | Overlap % |")
        out.append("|---|---|---|")
        for a, b, v in high:
            out.append(f"| {a} | {b} | {v:.1f}% |")
    elif pairs:
        out.append("**Largest scheme overlaps:**")
        out.append("")
        out.append("| Scheme A | Scheme B | Overlap % |")
        out.append("|---|---|---|")
        for a, b, v in pairs[:3]:
            out.append(f"| {a} | {b} | {v:.1f}% |")
    else:
        out.append("No material scheme-level overlaps detected.")
    out.append("")
    top5 = pa["effective_holdings"][:5]
    if top5:
        out.append("**Top holdings across the portfolio:**")
        out.append("")
        out.append("| Holding | ISIN | Sector | Weight % |")
        out.append("|---|---|---|---|")
        for h in top5:
            out.append(f"| {_md_cell(h['company'])} | {h.get('isin') or '-'} | "
                       f"{_md_cell(h.get('sector') or '-')} | {h['weight']:.2f}% |")
        out.append("")
    out.append("## 2. Proposed Realignment")
    out.append("")
    out.append("| # | Instrument | Type | Category | Allocation % | Proposed action | Rationale | Remarks |")
    out.append("|---|---|---|---|---|---|---|---|")
    for ln in lines:
        action = replacements.get(str(ln["index"]), "Retain" if ln["type"] == "scheme" else "Hold")
        remark = remarks.get(str(ln["index"]), "")
        out.append(f"| {ln['index'] + 1} | {_md_cell(ln['name'])} | {ln['type']} | "
                   f"{_md_cell(ln['category'])} | {ln['weight']:.1f}% | {_md_cell(action)} | "
                   f"{_md_cell(ln['rationale'])} | {_md_cell(remark)} |")
    out.append("")
    out.append("## 3. Key Rationale")
    out.append("")
    out.append("- Portfolio construction emphasises **diversification** and "
               "reduces hidden concentration in overlapping securities.")
    out.append("- Rebalancing reduces overlap-driven double exposure while "
               "preserving intended factor/asset-class exposure.")
    out.append("- Riskometer and expense alignment reviewed for suitability "
               "against the stated investment objective.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("> **Disclaimer:** This document is a diagnostic tool for factual "
               "analysis only and is **not investment advice**. Past performance is "
               "not indicative of future returns. Please consult a SEBI-registered "
               "investment adviser before making investment decisions.")
    return {"markdown": "\n".join(out), "lines": lines, "schemes": pa["schemes"],
            "stocks": pa["stocks"], "overlap": ov["matrix"], "asset_split": pa["asset_split"]}


def _today_str() -> str:
    from datetime import date
    return date.today().isoformat()


def _top_overlap_pairs(matrix: list[dict], k: int = 3) -> list[tuple]:
    pairs = []
    keys = [m["id"] for m in matrix]
    for i, m in enumerate(matrix):
        for j, k2 in enumerate(keys):
            if i >= j:
                continue
            v = m.get(f"c_{k2}", 0)
            pairs.append((m["scheme"], matrix[j]["scheme"], v))
    pairs.sort(key=lambda x: -x[2])
    return pairs[:k]


def _rationale(scheme: dict, overlap: dict) -> str:
    if scheme.get("n_holdings"):
        return f"Portfolio of {scheme['n_holdings']} holdings" + (
            f"; top holding {scheme.get('top_holding', '')} at "
            f"{scheme.get('top_holding_pct', 0):.2f}%." if scheme.get("top_holding_pct") else ".")
    return "Tracked index exposure with benchmark-aligned holdings."


def _opt_bool(v: str | None):
    if v is None:
        return None
    return v.lower() in ("1", "true", "yes", "on")


@app.get("/api/mapping")
def api_mapping(request: Request):
    _require_user(request)
    q = request.query_params.get("q", "").strip()
    if not q:
        return {"items": []}
    return {"items": get_db().mapping(q)}


@app.get("/api/filters")
def api_filters(request: Request):
    """Distinct filter facet values for the explorer UI."""
    _require_user(request)
    wdb = get_db()
    cur = wdb.con
    return {
        "amcs": [r[0] for r in cur.execute(
            "SELECT DISTINCT amc FROM schemes WHERE amc!='' ORDER BY amc")],
        "categories": [r[0] for r in cur.execute(
            "SELECT DISTINCT category FROM schemes ORDER BY category")],
        "sources": [r[0] for r in cur.execute(
            "SELECT DISTINCT source FROM schemes ORDER BY source")],
        "coverage": [r[0] for r in cur.execute(
            "SELECT DISTINCT coverage FROM schemes ORDER BY coverage")],
        "caps": [r[0] for r in cur.execute(
            "SELECT DISTINCT cap FROM securities WHERE cap!='na' ORDER BY cap")],
        "sectors": [r[0] for r in cur.execute(
            "SELECT DISTINCT sector FROM securities WHERE sector!='na' ORDER BY sector")],
    }


@app.get("/api/health")
def api_health():
    return {"status": "ok"}


@app.get("/api/version")
def api_version():
    """Which exact build is serving this request (Railway injects the git SHA)."""
    from datetime import timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return {
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", ""),
        "service": os.environ.get("RAILWAY_SERVICE_NAME", ""),
        "environment": os.environ.get("RAILWAY_ENVIRONMENT_NAME", ""),
        "scheduler_enabled": os.environ.get("ENABLE_SCHEDULER") == "1",
        "server_time_ist": datetime.now(ist).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Superadmin: refresh telemetry + manual job triggers
# --------------------------------------------------------------------------

def _require_superadmin(request: Request) -> dict:
    user = _require_user(request)
    if (user.get("email") or "").lower() not in SUPERADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Superadmin access required.")
    return user


_admin_running: set[str] = set()


def _admin_jobs() -> dict:
    from src.stock_refresh import refresh_all
    return {
        "nav_daily": _nav_job,
        "amfi_fetch": _amfi_job,
        "bond_refresh": _bond_job,
        "stock_refresh": lambda: refresh_all(daily=True),
    }


@app.get("/api/admin/refresh-summary")
def admin_refresh_summary(request: Request):
    _require_superadmin(request)
    from src.refresh_log import LOG_PATH, summary
    s = summary()
    return {"pipelines": s["pipelines"],
            "generated_at": s["generated_at"],
            "scheduler_enabled": os.environ.get("ENABLE_SCHEDULER") == "1",
            "running": sorted(_admin_running),
            "log_file": str(LOG_PATH)}


@app.get("/api/admin/refresh-logs")
def admin_refresh_logs(request: Request, limit: int = 200):
    _require_superadmin(request)
    from src.refresh_log import read
    return {"items": read(max(1, min(limit, 1000)))}


@app.post("/api/admin/run/{pipeline}")
def admin_run_job(pipeline: str, request: Request):
    _require_superadmin(request)
    jobs = _admin_jobs()
    if pipeline not in jobs:
        raise HTTPException(status_code=404,
                            detail=f"Unknown pipeline '{pipeline}'. Known: {sorted(jobs)}")
    if pipeline in _admin_running:
        raise HTTPException(status_code=409, detail=f"'{pipeline}' is already running.")
    _admin_running.add(pipeline)

    def runner() -> None:
        try:
            jobs[pipeline]()
        except Exception:  # errors are recorded by refresh_log.track
            pass
        finally:
            _admin_running.discard(pipeline)

    threading.Thread(target=runner, name=f"admin-{pipeline}", daemon=True).start()
    return {"status": "started", "pipeline": pipeline}


@app.get("/api/scope-stats")
def api_scope_stats():
    """PUBLIC data-scope snapshot for the sign-in page: how much data the
    engine holds, each with its non-N/A coverage percentage."""
    wdb = get_db()
    m = wdb.meta_stats()
    cat = wdb._bond_catalog()
    bonds = cat.get("bonds") or []
    n_bonds = len(bonds)
    n_ytm = sum(1 for b in bonds if b.get("ytm") is not None)
    n_price = sum(1 for b in bonds if b.get("price"))
    pct = lambda part, whole: round(part / whole * 100, 1) if whole else None  # noqa: E731
    return {
        "as_of": m.get("as_of"),
        "amcs": m.get("amcs"),
        "schemes": m.get("schemes"),
        "schemes_covered_pct": pct(m.get("schemes_with_holdings"), m.get("schemes")),
        "holdings": m.get("holdings"),
        "isin_pct": m.get("isin_completeness"),
        "stocks": m.get("pure_stocks"),
        "bonds": n_bonds,
        "bonds_ytm_pct": pct(n_ytm, n_bonds),
        "bonds_traded": n_price,
        "bond_as_of": cat.get("as_of"),
    }


@app.get("/api/stocks/status")
def api_stocks_status(request: Request):
    """Stock backfill completion status (price / actions / reports)."""
    _require_user(request)
    from src.stock_status import report
    return report()


# --------------------------------------------------------------------------
# Bonds (NSE debt market)
# --------------------------------------------------------------------------

@app.get("/api/bonds")
def api_bonds(request: Request):
    """All bonds traded/listed on the NSE debt market (G-Sec, SDL, T-Bill,
    PSU & corporate bonds) with coupon, maturity, last price and YTM
    (NSE-reported or computed from coupon+price+maturity)."""
    _require_user(request)
    p = request.query_params
    limit, offset = _page(request)
    return get_db().list_bonds(
        q=p.get("q"), segment=p.get("segment"), rating=p.get("rating"),
        status=p.get("status"), maturity=p.get("maturity"),
        only_traded=_opt_bool(p.get("only_traded")), sort=p.get("sort"),
        limit=limit, offset=offset)


@app.get("/api/bonds/meta")
def api_bonds_meta(request: Request):
    """Bond filter facets + summary for the Bonds tab."""
    _require_user(request)
    return get_db().bond_facets()


@app.get("/api/bonds/{isin}")
def api_bond_detail(isin: str, request: Request):
    _require_user(request)
    bond = get_db().get_bond(isin.upper())
    if not bond:
        raise HTTPException(status_code=404, detail="Bond not found.")
    return bond


# --------------------------------------------------------------------------
# Static pages
# --------------------------------------------------------------------------

_PAGES = {"": "index.html", "index.html": "index.html", "login": "login.html",
          "register": "register.html", "app": "app.html"}

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{page_name:path}", response_class=HTMLResponse)
def serve_page(page_name: str):
    if page_name.startswith("static/"):
        raise HTTPException(status_code=404, detail="Not found.")
    if page_name == "app.html" or page_name == "app":
        page = "app.html"
    else:
        page = _PAGES.get(page_name)
        if page is None and page_name.count("/") == 0:
            page = _PAGES.get(page_name.lower(), None)
        if page is None:
            page = "index.html"
    fpath = STATIC_DIR / page
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Page not found.")
    return FileResponse(fpath)


def run() -> None:
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run("webapp.main:app", host=host, port=port, reload=False)