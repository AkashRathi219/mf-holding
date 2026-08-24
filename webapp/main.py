"""FastAPI application for the FundPulse webapp.

Serves the static frontend and a JSON API over the built holdings database.
All data endpoints require a valid session token (login / register first).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db
from .log import get_logger, request_logging_middleware
from .ratelimit import (AUTH_LOGIN_LIMITER, AUTH_REGISTER_LIMITER,  # [H1]
                        SlidingWindowRateLimiter, enforce)
from .remote_store import ensure as remote_ensure
from src.stock_refresh import refresh_all  # stdlib-only chain; scheduler wiring
from .tools_api import router as tools_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

log = get_logger("main")

SUPERADMIN_EMAILS = {e.strip().lower() for e in os.environ.get(
    "SUPERADMIN_EMAILS", "akash@aracharatventures.com").split(",") if e.strip()}

app = FastAPI(title="FundPulse", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.include_router(tools_router)


def _add_cors(app, origins: str | None = None) -> None:
    """[H3] Cross-origin API access for the marketing site, env-driven.

    Set CORS_ORIGINS="https://aracharatventures.com,https://www.…". Empty
    (default) = same-origin only, no CORS headers at all. Credentials stay
    disabled — auth is header-based (Authorization/X-Auth-Token), not cookies.
    """
    from fastapi.middleware.cors import CORSMiddleware

    raw = origins if origins is not None else os.environ.get("CORS_ORIGINS", "")
    allowed = [o.strip() for o in raw.split(",") if o.strip()]
    if not allowed:
        return
    if "*" in allowed:
        log.warning("CORS_ORIGINS contains '*' — allowing all origins; "
                    "prefer an explicit allowlist in production")
        allowed = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Auth-Token",
                       "X-Request-ID"],
        max_age=86400,
    )


_add_cors(app)


# --------------------------------------------------------------------------
# Scheduled jobs (opt-in via ENABLE_SCHEDULER=1; set on Railway)
# --------------------------------------------------------------------------

_job_runner_started = False


def _amfi_job() -> dict:
    from src.refresh_log import track
    with track("amfi_fetch") as _meta:
        try:
            from webapp.amfi_fetch import fetch_mfdata, save
        except Exception as e:  # missing dep must surface, not vanish [S2e]
            log.warning("amfi_fetch unavailable: %s", e,
                        extra={"path": "/job/amfi_fetch", "status": 0})
            _meta["error"] = f"import failed: {e}"
            return {"error": str(e)[:120]}
        data = fetch_mfdata()
        paths = save(data, "latest") if data else []
        _meta.update(amcs=len(data), files=len(paths))
        # Piggyback: verify the AMC registry against AMFI's official
        # portfolio-disclosure directory (monthly cadence is right for this).
        try:
            from webapp.amfi_portal import refresh_registry, scrape_members
            rep = refresh_registry(scrape_members())
            _meta.update(directory_verified=rep["portal_members"],
                         directory_filled=rep["filled_empty"])
        except Exception as e:  # never break the holdings fetch
            _meta["directory_error"] = str(e)[:120]
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


def _nav_job(days: int = 7, mode: str = "scheduled") -> dict:
    """Daily NAV refresh + per-scheme gap-fill from last-known date to today.

    Accepts ``days`` because the scheduler resolves the lookback window from
    settings (``scheduler.nav_refresh.days``) and passes it here [S2c].
    ``mode`` records what actually triggered the run ("scheduled" cron vs
    "manual" superadmin click) so telemetry never mislabels a run [NAV-STUB
    follow-up: the 24-Aug incident run was a manual click logged as
    "scheduled"]."""
    from src.refresh_log import track
    with track("nav_daily", mode=mode) as meta:
        try:
            from .remote_store import ensure_prefix
            meta["universe_pulled"] = ensure_prefix("universe")
        except Exception:
            pass
        from src.nav_daily import (_update_latest_navs_impl,
                                   fill_gaps_from_last_known)
        s1 = _update_latest_navs_impl(days=days)  # impl: no nested telemetry event
        meta.update(s1)
        gaps = fill_gaps_from_last_known()
        meta.update({f"gap_{k}": v for k, v in gaps.items()})
        # Piggyback: AMFI SIF latest-NAV snapshot (daily data, tiny JSON).
        try:
            from webapp.amfi_portal import save_sif_nav
            doc = save_sif_nav()
            meta["sif_rows"] = doc["count"]
            s1["sif_rows"] = doc["count"]
        except Exception as e:  # never break the NAV refresh
            meta["sif_error"] = str(e)[:120]
        return {**s1, "gapfill": gaps}


def _noop_pipeline(*args, **kwargs) -> None:
    # Monthly AMC-site downloads stay out of the web container (heavy PDFs);
    # they run from a workstation via `python main.py run` / the CLI scheduler.
    from src.refresh_log import record
    record("monthly_holdings_fetch", "skipped",
           note="runs from workstation pipeline, not the web container")


def _preheal_job() -> dict:
    """Bounded sweep upgrading thin nav_history files from R2/mirror [NAV-STUB]."""
    from webapp.db import preheal_nav_stubs
    return preheal_nav_stubs(limit=500)


def _start_scheduler_thread() -> None:
    """Run the cron jobs inside the web process when ENABLE_SCHEDULER=1.

    Import failures here must never take the web tier down [S2b]: the
    startup hook wraps this call, and the thread body logs instead of dying
    silently.
    """
    global _job_runner_started
    if _job_runner_started or os.environ.get("ENABLE_SCHEDULER") != "1":
        return
    import asyncio

    try:
        import yaml  # noqa: F401  (declared so a missing dep fails loudly HERE)
        from src.scheduler import MonthlyScheduler
    except Exception as e:
        log.error("scheduler disabled — dependency import failed: %s", e)
        return

    def _loop() -> None:
        async def runner() -> None:
            try:
                config = {}
                cfg_path = BASE_DIR.parent / "config" / "settings.yaml"
                try:
                    config = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                except Exception as e:
                    log.warning("settings.yaml unreadable, scheduler defaults in use: %s", e)
                sched = MonthlyScheduler(
                    _noop_pipeline, config, base_dir=BASE_DIR.parent,
                    nav_refresh_fn=_nav_job,
                    stock_refresh_fn=refresh_all,
                    bond_refresh_fn=_bond_job,
                    amfi_fn=_amfi_job,
                    preheal_fn=_preheal_job,
                )
                sched.start()
            except Exception:
                log.exception("scheduler failed to start; continuing without cron jobs")
                return
            while True:
                await asyncio.sleep(3600)

        asyncio.run(runner())

    threading.Thread(target=_loop, name="job-scheduler", daemon=True).start()
    _job_runner_started = True


@app.on_event("startup")
def _on_startup() -> None:
    try:  # [S2b] a dead scheduler must degrade, never brick the deployment
        _start_scheduler_thread()
    except Exception:
        log.exception("scheduler startup failed; web tier continues without it")
    try:  # [H4] surface a missing SECRET_KEY at boot, not on first login
        auth._get_secret()
    except auth.SecretNotConfiguredError as e:
        log.critical("SECRET_KEY NOT CONFIGURED: %s — login/register will "
                     "return 503 until the env var is set", e)


@app.middleware("http")
async def _request_logging(request: Request, call_next):
    return await request_logging_middleware(request, call_next)


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
    try:
        user = auth.user_from_token(token)
    except auth.SecretNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not user:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    return user


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def api_register(request: Request, body: RegisterIn):
    enforce(AUTH_REGISTER_LIMITER, request)  # [H1]
    try:
        return auth.register_user(body.email, body.name, body.org, body.password)
    except auth.SecretNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login")
def api_login(request: Request, body: LoginIn):
    enforce(AUTH_LOGIN_LIMITER, request)  # [H1] PBKDF2@200k makes unthrottled
    try:  # attempts a cheap CPU-exhaustion vector as well as a brute-force one
        return auth.login_user(body.email, body.password)
    except auth.SecretNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/auth/logout-all")
def api_logout_all(request: Request):
    """Kill switch [H2]: invalidates EVERY token issued to this user."""
    user = _require_user(request)
    auth.revoke_user_tokens(user["id"])
    log.info("tokens revoked for user %s", user.get("email"))
    return {"status": "ok", "revoked": True}


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
    items = data.get("items") or []
    if items:
        from webapp import data_health
        stats = get_db().holdings_stats([s["id"] for s in items])
        for s in items:
            s["confidence"] = data_health.scheme_confidence(s, stats.get(s["id"]))
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
    try:
        from webapp import data_health
        st = get_db().holdings_stats([scheme_id]).get(scheme_id)
        out["confidence"] = data_health.scheme_confidence(out, st)
    except Exception:
        pass
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


@app.get("/api/schemes/{scheme_id}/analytics")
def api_scheme_analytics(scheme_id: int, request: Request):
    """Performance & risk suite (CAGR windows, volatility, Sharpe/Sortino,
    max drawdown, rolling-1Y distribution, benchmark-relative stats) over the
    scheme's AMFI NAV history [ANA1]. Factual computations only — every figure
    carries its as-of date and window; never a recommendation."""
    _require_user(request)
    out = get_db().scheme_analytics(scheme_id)
    if not out:
        raise HTTPException(status_code=404, detail="Scheme not found.")
    return out


@app.post("/api/schemes/compare")
def api_schemes_compare(request: Request, body: dict):
    """Compare 2-12 schemes side by side: metric table + growth-of-R100 +
    rolling-1Y series over a common window [ANA2]. Factual NAV math only."""
    _require_user(request)
    ids = body.get("scheme_ids") or []
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(status_code=400,
                            detail="Provide scheme_ids: at least two scheme ids.")
    if len(ids) > 12:
        raise HTTPException(status_code=400, detail="At most 12 schemes per comparison.")
    return get_db().compare_schemes(ids)


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


FEEDBACK_PATH = BASE_DIR.parent / "data" / "feedback.json"
FEEDBACK_R2_KEY = "logs/feedback.json"
_feedback_lock = threading.Lock()


def _load_feedback() -> list:
    """Read feedback records, restoring from R2 on a fresh container [S4]."""
    try:
        if not FEEDBACK_PATH.exists():
            remote_ensure(FEEDBACK_R2_KEY, dest=FEEDBACK_PATH)
    except Exception as e:
        log.warning("feedback R2 restore failed: %s", e)
    records: list = []
    if FEEDBACK_PATH.exists():
        try:
            loaded = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                records = loaded
        except Exception as e:
            log.warning("feedback.json unreadable, starting clean: %s", e)
            records = []
    return records


@app.post("/api/feedback")
def api_feedback(request: Request, body: dict):
    """Capture feature-feedback (portfolio tools + try page) into
    data/feedback.json — atomic write, lock-protected, best-effort pushed to
    R2 so submissions survive redeploys [S4]."""
    user = _require_user(request)
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Feedback message is required.")
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "user": (user.get("email") or user.get("name") or ""),
        "message": message,
        "context": body.get("context") or "",
    }
    with _feedback_lock:
        records = _load_feedback()
        records.append(entry)
        try:
            FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = FEEDBACK_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(FEEDBACK_PATH)
        except Exception as e:
            log.error("feedback write failed: %s", e)
            raise HTTPException(status_code=500, detail="Could not store feedback.")
        try:
            from .remote_store import is_configured, upload_object
            if is_configured():
                upload_object(FEEDBACK_R2_KEY, FEEDBACK_PATH)
        except Exception as e:  # local copy remains the source of truth
            log.warning("feedback R2 push failed: %s", e)
    return {"status": "ok", "total": len(records)}


@app.get("/api/admin/feedback")
def admin_feedback(request: Request, limit: int = 200):
    """Reader side of the feedback loop [L3] — newest last."""
    _require_superadmin(request)
    records = _load_feedback()
    return {"total": len(records), "items": records[-max(1, min(limit, 1000)):]}


# --------------------------------------------------------------------------
# Try App waitlist (public; marketing-site investors page) [WEB2]
# --------------------------------------------------------------------------

WAITLIST_PATH = BASE_DIR.parent / "data" / "waitlist.json"
WAITLIST_R2_KEY = "logs/waitlist.json"
_waitlist_lock = threading.Lock()
WAITLIST_LIMITER = SlidingWindowRateLimiter(max_events=5, window_seconds=3600)
_EMAIL_OK_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-@")


def _load_waitlist() -> list:
    """Read waitlist records, restoring from R2 on a fresh container [WEB2]."""
    try:
        if not WAITLIST_PATH.exists():
            remote_ensure(WAITLIST_R2_KEY, dest=WAITLIST_PATH)
    except Exception as e:
        log.warning("waitlist R2 restore failed: %s", e)
    records: list = []
    if WAITLIST_PATH.exists():
        try:
            loaded = json.loads(WAITLIST_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                records = loaded
        except Exception as e:
            log.warning("waitlist.json unreadable, starting clean: %s", e)
            records = []
    return records


@app.post("/api/try/waitlist")
def api_try_waitlist(request: Request, body: dict):
    """Capture a waitlist email from the public investors page.

    No auth (public funnel), per-IP rate limited [H1 limiter], atomic write,
    lock-protected, best-effort pushed to R2 so signups survive redeploys.
    Idempotent per email — repeats return ok without duplicating rows."""
    enforce(WAITLIST_LIMITER, request)
    email = (body.get("email") or "").strip().lower()
    if not email or len(email) > 254 or "@" not in email or \
            set(email) - _EMAIL_OK_CHARS or \
            email.count("@") != 1 or \
            not email.split("@")[1].strip(".") or not email.split("@")[0]:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "email": email,
             "source": (body.get("source") or "investors")[:40]}
    with _waitlist_lock:
        records = _load_waitlist()
        duplicated = any(r.get("email") == email for r in records)
        if not duplicated:
            records.append(entry)
            try:
                WAITLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = WAITLIST_PATH.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(WAITLIST_PATH)
            except Exception as e:
                log.error("waitlist write failed: %s", e)
                raise HTTPException(status_code=500, detail="Could not store signup.")
            try:
                from .remote_store import is_configured, upload_object
                if is_configured():
                    upload_object(WAITLIST_R2_KEY, WAITLIST_PATH)
            except Exception as e:  # local copy remains the source of truth
                log.warning("waitlist R2 push failed: %s", e)
    return {"status": "ok", "total": len(records)}


@app.get("/api/admin/waitlist")
def admin_waitlist(request: Request, limit: int = 200):
    """Reader side of the waitlist — newest last."""
    _require_superadmin(request)
    records = _load_waitlist()
    return {"total": len(records), "items": records[-max(1, min(limit, 1000)):]}


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
    out.append(f"_Generated on {_today_str()} - FundPulse_")
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
    out.append("## 3. Performance snapshot (factual)")
    out.append("")
    out.append("Trailing CAGR windows, annualised volatility and maximum drawdown "
               "computed from official AMFI NAV history. Figures are factual "
               "computations, percentile-neutral, and never a recommendation.")
    out.append("")
    perf_rows: list[list[str]] = []
    from .analytics import DEFAULT_RF_PCT as _RF, METHODOLOGY_VERSION
    for ln in lines:
        if ln["type"] != "scheme" or not ln.get("resolved"):
            continue
        try:
            sid = next((s["id"] for s in pa["schemes"]
                        if s["fund_name"] == ln["name"]), None)
            a = wdb.scheme_analytics(sid) if sid else {}
        except Exception:  # noqa: BLE001 — performance is additive, never fatal
            a = {}
        c = (a.get("cagr_pct") or {})
        rk = (a.get("risk") or {})
        perf_rows.append([
            ln["name"], f"{ln['weight']:.1f}%",
            f"{c['y1']:.2f}%" if c.get("y1") is not None else "—",
            f"{c['y3']:.2f}%" if c.get("y3") is not None else "—",
            f"{c['y5']:.2f}%" if c.get("y5") is not None else "—",
            f"{rk.get('volatility_pct'):.2f}%" if rk.get("volatility_pct") is not None else "—",
            f"{rk.get('max_drawdown_pct'):.2f}%" if rk.get("max_drawdown_pct") is not None else "—",
        ])
    if perf_rows:
        out.append("| Scheme | Alloc % | 1Y CAGR | 3Y CAGR | 5Y CAGR | Volatility ann.* | Max drawdown* |")
        out.append("|---|---|---|---|---|---|---|")
        for row in perf_rows:
            out.append("| " + " | ".join(_md_cell(v) for v in row) + " |")
        out.append("")
        out.append(f"*3-year window where history allows; \"—\" = insufficient NAV "
                   f"history, never zero. Risk-free rate assumed "
                   f"{_RF:.1f}% (documented assumption). As-of dates and windows are "
                   f"intrinsic to each computation.")
    else:
        out.append("_No scheme in this proposal has enough NAV history for the "
                   "performance engine yet._")
    out.append("")
    out.append(f"_Methodology stamp: `{METHODOLOGY_VERSION}` · generated "
               f"{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')} · "
               f"formulas and conventions documented on the FundPulse data-methodology page._")
    out.append("")
    out.append("## 4. Key Rationale")
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
    out.append("> **Disclaimer (locked):** This document is a diagnostic tool for factual "
               "analysis only and is **not investment advice**. Past performance is "
               "not indicative of future returns. Performance figures are factual "
               "computations over public AMFI NAV data with their computation windows "
               "stated alongside; they are not forecasts and create no expectation of "
               "future results. Please consult a SEBI-registered "
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


_health_cache: tuple[float, dict] | None = None
_HEALTH_TTL_S = 10.0


def _prod_intent() -> bool:
    """True when this deployment is EXPECTED to serve the full dataset
    (Railway-style). In that mode a missing/broken DB is a hard 503; on a dev
    box with no R2 and no readonly flag an absent DB is just 'no data yet'."""
    return bool(os.environ.get("MF_READONLY_DB") == "1" or
                os.environ.get("R2_ACCOUNT_ID"))


def _health_probe() -> dict:
    """Deep readiness probe [S3]: the deployment gate must reflect reality.

    503 (via caller) when serving would be pointless — primary DB unreadable/
    empty — but ONLY when prod-intent is declared (`MF_READONLY_DB=1` or R2
    configured). A bare dev checkout without data returns 200 + degraded
    flags instead of crash-looping.
    """
    checks: dict = {}
    ok = True
    try:
        wdb = get_db()
        n_schemes = wdb.con.execute("SELECT COUNT(*) FROM schemes").fetchone()[0]
        checks["db"] = {"ok": n_schemes > 0, "schemes": n_schemes}
        if n_schemes <= 0:
            ok = False
    except Exception as e:
        log.error("health probe: DB unreadable: %s", e)
        checks["db"] = {"ok": False, "error": str(e)[:120]}
        ok = False
    from .remote_store import is_configured
    r2 = is_configured()
    checks["r2_configured"] = r2
    hb_age = _scheduler_heartbeat_age_s()
    checks["scheduler"] = {
        "enabled": os.environ.get("ENABLE_SCHEDULER") == "1",
        "heartbeat_age_s": hb_age,
        "ok": hb_age is not None and hb_age < 25 * 3600,
    }
    status = "ok" if ok else ("degraded" if not _prod_intent() else "error")
    if not ok and _prod_intent():
        log.error("health probe FAILED in prod-intent mode: %s", checks["db"])
    return {"status": status, "checks": checks,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}, \
        (ok or not _prod_intent())


def _scheduler_heartbeat_age_s() -> float | None:
    """Seconds since the scheduler last recorded an 'alive' heartbeat (S2f)."""
    try:
        from src.refresh_log import read_state
        entry = (read_state().get("pipelines") or {}).get("scheduler") or {}
        ts = entry.get("last_ts")
        if not ts:
            return None
        ist = timezone(timedelta(hours=5, minutes=30))
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ist)
        return round((datetime.now(ist) - dt).total_seconds(), 1)
    except Exception:
        return None


@app.get("/api/health")
def api_health():
    global _health_cache
    import time as _time
    now = _time.monotonic()
    if _health_cache and (now - _health_cache[0]) < _HEALTH_TTL_S:
        payload, serving_ok = _health_cache[1]
    else:
        payload, serving_ok = _health_probe()
        _health_cache = (now, (payload, serving_ok))
    if not serving_ok:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/version")
def api_version():
    """Which exact build is serving this request (Railway injects the git SHA)."""
    ist = timezone(timedelta(hours=5, minutes=30))  # [S1] datetime now module-level
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
        "nav_daily": lambda: _nav_job(mode="manual"),
        "nav_preheal": _preheal_job,
        "amfi_fetch": _amfi_job,
        "bond_refresh": _bond_job,
        "stock_refresh": lambda: refresh_all(daily=True),
    }


@app.get("/api/admin/refresh-summary")
def admin_refresh_summary(request: Request):
    _require_superadmin(request)
    from src.refresh_log import summary
    s = summary()
    return {"pipelines": s["pipelines"],
            "generated_at": s["generated_at"],
            "scheduler_enabled": os.environ.get("ENABLE_SCHEDULER") == "1",
            "running": sorted(_admin_running),
            "log_file": s["log_file"],
            "state_file": s["state_file"]}


@app.get("/api/admin/data-health")
def admin_data_health(request: Request, force: int = 0):
    """Composite data-health score (0-100) with per-component breakdown."""
    _require_superadmin(request)
    from webapp import data_health
    return data_health.compute(force=bool(force))


@app.get("/api/admin/data-health-history")
def admin_data_health_history(request: Request, limit: int = 100):
    _require_superadmin(request)
    from webapp import data_health
    return {"items": data_health.read_history(max(1, min(limit, 500)))}


@app.get("/api/admin/reliance")
def admin_reliance(request: Request):
    """Per-scheme data-confidence rollup across ALL schemes."""
    _require_superadmin(request)
    from webapp import data_health
    return data_health.reliance_metrics(get_db())


@app.get("/api/admin/nav-freshness")
def admin_nav_freshness(request: Request, sample: int = 5, live: int = 0,
                        stocks: int = 0):
    """Every scheme's NAV date vs the AMFI publication calendar [NAV-FRESH].

    Buckets: current / lag1 (grace) / stale_recent / stale_deep /
    dead_suspect / no_history. `live=1` additionally three-way spot-checks
    `sample` random schemes (history file vs live AMFI vs DB) — slower.
    """
    _require_superadmin(request)
    from src.nav_audit import run_audit
    return run_audit(sample=max(0, min(sample, 25)), live=bool(live),
                     csv_out=None, with_stocks=bool(stocks))


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
        else:
            try:
                from webapp import data_health
                data_health.invalidate()
            except Exception:
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