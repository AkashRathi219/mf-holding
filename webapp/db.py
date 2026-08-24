"""Data layer for the FundPulse webapp.

Loads the parsed holdings / universe / securities / index-reference files under
``data/`` into a single cached SQLite database (``data/webapp.db``), then exposes
query helpers for the API layer.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import remote_store
from src.amfi_nav import fund_name_from_nav

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "webapp.db"

# Folders scanned for scheme-indexed holdings JSON (excel / zip / html parsers)
AMC_WEBSITES_DIR = DATA_DIR / "parsed" / "amc_websites"
ADVISORKHOJ_DIR = DATA_DIR / "parsed" / "advisorkhoj"
AMFI_DIR = DATA_DIR / "parsed" / "amfi"
UNIVERSE_CSV = DATA_DIR / "universe" / "Combined NAV - 14-Aug-2026.csv"
EQUITY_ISINS_CSV = DATA_DIR / "reference" / "equity_isins.csv"
INDEX_RESOLVED_JSON = DATA_DIR / "reference" / "index_resolved_holdings.json"
NAV_HISTORY_DIR = DATA_DIR / "nav_history"
STOCK_HISTORY_DIR = DATA_DIR / "stock_history"
STOCK_ACTIONS_DIR = DATA_DIR / "stock_actions"
STOCK_REPORTS_DIR = DATA_DIR / "stock_reports"
DISCOVERY_NEEDED_CSV = DATA_DIR / "reference" / "discovery_needed.csv"
NO_DISCLOSURE_CSV = DATA_DIR / "reference" / "no_disclosure.csv"
# Optional benchmark-weight file for index/ETF funds, keyed by Nifty index name:
#   data/nifty/weights.json  ->  { "Nifty200_Momentum_30": { "INE117A01022": 4.31, ... } }
# Ingestion source: niftyindices.com "Market Capitalisation & Weightage" monthly
# report / API (get_index_constituents returns per-stock weight + ISIN).
NIFTY_WEIGHTS_JSON = DATA_DIR / "nifty" / "weights.json"
# Total-return index series for benchmark-relative analytics [ANA1]
NIFTY_TR_DIR = DATA_DIR / "nifty" / "TR"
# Bond-market universe built by src/bonds.py (NSE bulk files + computed YTM)
BONDS_CATALOG_JSON = DATA_DIR / "reference" / "bonds_catalog.json"

_WS = re.compile(r"\s+")
_NONALNUM = re.compile(r"[^a-z0-9]+")

# nav_history self-heal [NAV-STUB]: a local history file below this many points
# is treated as a possibly-thin stub (e.g. written by an old nav_daily cold
# start) that may be shadowing the full R2 object. The first read attempts one
# upgrade per code per process; genuinely young funds keep their short (honest)
# history when no better copy exists anywhere.
NAV_STUB_HEAL_MIN_POINTS = 30
_nav_heal_attempted: set[str] = set()

# scheme_analytics result cache [perf]: keyed by (scheme, plan, last NAV
# date, rf, benchmark-file mtime) — a fresh WebDB is built per request, so
# this MUST live at module level to survive across calls. FIFO-bounded.
_analytics_cache: dict[tuple, dict] = {}
_ANALYTICS_CACHE_MAX = 512


def _heal_thin_nav_history(code: str, local_points: int) -> dict | None:
    """Try to upgrade a thin nav_history file to a full one; return the better
    doc (already persisted) or None.

    Order: (1) force-download the R2 object to a temp file and keep it only if
    it has MORE points than the local copy (never lose good local data to a
    worse remote); (2) the mfapi full-history mirror, once per code per
    process. The once-guard keeps request-path cost bounded: at most one
    download + one mirror call per thin file per process lifetime."""
    if code in _nav_heal_attempted:
        return None
    _nav_heal_attempted.add(code)
    path = NAV_HISTORY_DIR / f"{code}.json"
    tmp = path.parent / (path.name + ".heal")
    try:
        got = remote_store.download_to(f"nav_history/{code}.json", tmp)
        if got is not None:
            cand = json.loads(tmp.read_text(encoding="utf-8"))
            if len(cand.get("history") or []) > local_points:
                tmp.replace(path)
                return cand
            tmp.unlink()
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    try:
        from src.fetch_missing_nav import _build_doc, _fetch
        resp = _fetch(code)
        if resp is not None:
            cand = _build_doc(code, resp)
            if len(cand.get("history") or []) > local_points:
                NAV_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(cand), encoding="utf-8")
                return cand
    except Exception:
        pass
    return None


def preheal_nav_stubs(limit: int = 500) -> dict:
    """Proactively upgrade thin (<30 pt) nav_history files from R2/mirror.

    The read path heals lazily on first request; after a cold-start stub
    incident that leaves thousands of schemes paying a one-time heal on their
    first visitor. This sweep does the same work upfront, bounded per run.
    Reuses ``_heal_thin_nav_history`` (once-per-code-per-process guard), so a
    code with no better copy anywhere costs exactly one R2 miss + one mirror
    miss per process — genuinely young funds stay honestly thin."""
    thin: list[tuple[str, int]] = []
    if NAV_HISTORY_DIR.is_dir():
        for path in sorted(NAV_HISTORY_DIR.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            n = len(doc.get("history") or [])
            if 0 < n < NAV_STUB_HEAL_MIN_POINTS:
                thin.append((path.stem, n))
    healed = 0
    for code, n in thin[:max(0, limit)]:
        if _heal_thin_nav_history(code, n) is not None:
            healed += 1
    return {"scanned_thin": len(thin), "attempted": min(len(thin), max(0, limit)),
            "healed": healed, "deferred": max(0, len(thin) - max(0, limit))}


def norm_name(name: str) -> str:
    """Normalize a scheme/company name for fuzzy matching."""
    if not name:
        return ""
    return _NONALNUM.sub("", _WS.sub(" ", str(name)).lower())


_PLAN_TOKENS = {
    "direct", "dir", "regular", "reg", "growth", "g", "dividend", "div",
    "idcw", "idcwm", "idcwn", "bonus", "annual", "plan",
}


def strip_plan(name: str) -> str:
    """Remove plan/option suffixes (e.g. '- Direct (G)', ' (IDCW)') from a fund
    name so plan-level universe rows can be linked to fund-level holdings."""
    if not name:
        return name or ""
    s = str(name).strip()
    changed = True
    while changed:
        changed = False
        # trailing parenthetical option: "... (G)" / "... (IDCW)"
        m = re.search(r"[\(\[]([^)\]]+)[\)\]]\s*$", s)
        if m:
            inner = m.group(1).strip()
            inner_norm = re.sub(r"[^A-Za-z]+", "", inner).lower()
            is_plan = bool(_PLAN_TOKENS.intersection(re.split(r"[^A-Za-z]+", inner.lower())))
            # long/multi-word parentheticals are descriptive, not plan options
            is_descriptive = len(inner) > 24 or ("." in inner) or (len(inner.split()) > 4)
            if is_plan or is_descriptive:
                s = s[: m.start()].rstrip(" -–—:·")
                changed = True
                continue
        # trailing dash-separated plan token: "... - Direct" / "... - Reg (G)"
        m = re.search(r"[-–—]\s*([A-Za-z]+(?:\s*\([^)]*\))?)\s*$", s)
        if m:
            token = re.sub(r"[^A-Za-z]+", "", m.group(1)).lower()
            if token in _PLAN_TOKENS or token == "idcw" or token.startswith("idcw"):
                s = s[: m.start()].rstrip(" -–—:·")
                changed = True
    return s.strip()


def strip_plan_navall(name: str) -> str:
    """Lighter plan-stripping for AMFI navall names (e.g. 'X - Direct Plan - Growth',
    'X - Direct - Growth Option', 'X - Regular - IDCW'). Only removes trailing
    plan/option words; safe for general fund names."""
    if not name:
        return name or ""
    s = str(name).strip()
    while True:
        toks = s.split()
        if not toks:
            break
        last = re.sub(r"[^A-Za-z]+", "", toks[-1]).lower()
        if last in ("direct", "regular", "growth", "plan", "g", "idcw", "dividend",
                    "div", "monthly", "daily", "weekly", "quarterly", "annual",
                    "half", "reinvestment", "reinvest", "payout", "bonus", "option",
                    "incomedistributioncumcapitalwithdrawal", "cumcapitalwithdrawal",
                    "income", "distribution", "capital", "withdrawal") \
                or last.startswith("idcw") or last.startswith("income"):
            s = " ".join(toks[:-1]).rstrip(" -–—:·")
        else:
            break
    return s.strip()


# AMC brand prefix -> canonical brand prefix (normalized, no spaces).
# Only the LEADING brand token is rewritten, so it cannot corrupt the rest of
# the name (e.g. "ICICI Pru" -> "ICICI Prudential" must not touch "Prudential").
_BRAND_PREFIXES = {
    "adityabirlasunlife": "adityabirlasunlife",
    "adityabirlasl": "adityabirlasunlife",
    "adityabirla": "adityabirlasunlife",
    "abs": "adityabirlasunlife",
    "iciciprudential": "iciciprudential",
    "icicipru": "iciciprudential",
    "icici": "iciciprudential",
    "kotakmahindra": "kotak",
    "nipponindia": "nippon",
    "motilaloswal": "motilal",
    "mahindramanulife": "mahindra",
    "franklintempleton": "franklin",
    "canararobeco": "canararobeco",
    "barodabnp": "barodabnp",
    "360one": "360one",
    "thewealthcompany": "thewealthcompany",
    "oldbridge": "oldbridge",
    "angelone": "angelone",
    "jmfinancial": "jmfinancial",
    "whiteoak": "whiteoak",
    "jioblackrock": "jioblackrock",
    "capitalmind": "capitalmind",
}


def canon_name(name: str) -> str:
    """Plan-stripped, brand-normalized key for fund-name matching."""
    n = norm_name(strip_plan(name))
    for prefix, canon in sorted(_BRAND_PREFIXES.items(), key=lambda kv: -len(kv[0])):
        if n.startswith(prefix):
            return canon + n[len(prefix):]
    return n


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("₹", "").strip()
    if not s or s.lower() in ("na", "n/a", "-", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def float_state(value) -> bool:
    """True when a value parses as a positive finite number (e.g. a bond price)."""
    v = _num(value)
    return v is not None and v > 0


def _as_of_date(value) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if not s or s.lower() in ("na", "n/a", "none"):
        return ""
    s = s.replace(" 00:00:00", "").replace("00:00:00", "")
    # "as on 31st May 2026" / "July 31, 2026" style dates
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", s)
    if m:
        months = {mo: i for i, mo in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
        mm = months.get(m.group(2).lower()[:3])
        if mm:
            return f"{m.group(3)}-{mm:02d}-{int(m.group(1)):02d}"
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        months = {mo: i for i, mo in enumerate(
            ["january", "february", "march", "april", "may", "june", "july", "august",
             "september", "october", "november", "december"], 1)}
        mm = months.get(m.group(1).lower())
        if mm:
            return f"{m.group(3)}-{mm:02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return s


# --------------------------------------------------------------------------
# Source fingerprinting (rebuild the cache when any source file changes)
# --------------------------------------------------------------------------

def _fingerprint() -> str:
    h = hashlib.sha256()
    files: list[Path] = [
        UNIVERSE_CSV, EQUITY_ISINS_CSV, INDEX_RESOLVED_JSON,
        DISCOVERY_NEEDED_CSV, NO_DISCLOSURE_CSV,
    ]
    for d in (AMC_WEBSITES_DIR, ADVISORKHOJ_DIR, AMFI_DIR):
        if d.is_dir():
            files += sorted(p for p in d.rglob("*.json") if not p.name.startswith("report_"))
    for p in files:
        try:
            st = p.stat()
            h.update(p.as_posix().encode())
            h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            pass
    return h.hexdigest()


def _source_files() -> list[Path]:
    files: list[Path] = [UNIVERSE_CSV, EQUITY_ISINS_CSV, INDEX_RESOLVED_JSON,
                         DISCOVERY_NEEDED_CSV, NO_DISCLOSURE_CSV]
    for d in (AMC_WEBSITES_DIR, ADVISORKHOJ_DIR, AMFI_DIR):
        if d.is_dir():
            files += sorted(p for p in d.rglob("*.json") if not p.name.startswith("report_"))
    return [p for p in files if p.exists()]


_CODE_RE = re.compile(r"^[A-Z0-9]{1,5}$")
_JUNK_NAME_RE = re.compile(
    r"^(?:sheet\d+|top\s+\d+\s+holdings?|product\s+labell|please\s+mention|"
    r"name\s+of\s+the\s+(?:instrument|security)|industry\s+classification|"
    r"portfolio\s+summary|grand\s+total|net\s+assets|market\s+value\s+of\s+"
    r"investments?|total\s+amount\s+invested|returns?\b)\b", re.IGNORECASE)


def _is_junk_scheme_name(name: str, source: str) -> bool:
    """Sheet-code artifacts from the excel/zip parsers (e.g. Groww 'NI', 'AR',
    Jio 'JBNNX50', Samco 'SAMSCF') and header/instruction rows that slipped in
    as scheme names ('Sheet1', 'TOP 10 HOLDINGS BY ISSUER', 'Product Labelling')."""
    n = (name or "").strip()
    if not n:
        return True
    if " " in n:
        return bool(_JUNK_NAME_RE.match(n))
    # no spaces -> a sheet code unless it is a real, lower-cased fund name
    return bool(re.match(r"^[A-Z0-9]{1,15}$", n))


def _load_amc_website_schemes() -> Iterable[tuple[str, str, str, dict]]:
    """Yield (amc, source, as_of, scheme_payload) from amc_websites JSON files.

    Only files carrying a ``schemes`` mapping are used; PDF-only parses (which
    lack scheme attribution) are ignored because they don't map to the universe.
    Factsheet files are EXCLUDED from holdings: they carry only a partial
    top-holdings snapshot with mangled names, and are used for returns/benchmark
    extraction instead.
    """
    if not AMC_WEBSITES_DIR.is_dir():
        return
    for p in sorted(AMC_WEBSITES_DIR.rglob("*.json")):
        if p.name.startswith("report_"):
            continue
        if "factsheet" in p.name.lower():
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if isinstance(doc.get("metadata"), dict) and doc["metadata"].get("grouped_factsheet"):
            continue
        schemes = doc.get("schemes")
        if not isinstance(schemes, dict) or not schemes:
            continue
        amc = (doc.get("amc_name") or p.parent.name).replace("_", " ").strip()
        for code, payload in schemes.items():
            if not isinstance(payload, dict):
                continue
            fund = payload.get("fund_name") or payload.get("scheme_name") or ""
            holdings = payload.get("holdings")
            if not isinstance(holdings, list) or not holdings:
                continue
            # 6th element = the scheme dict key (may be a sheet code like 'PPFCF');
            # the caller decides whether to treat it as a real name or a code.
            yield amc, "amc_website", payload.get("date"), payload, fund, code


def _load_amc_website_code_schemes() -> Iterable[tuple[str, str, str, dict, str]]:
    """Like _load_amc_website_schemes but only for sheet-code-keyed schemes."""
    for amc, source, as_of, payload, fund, code in _load_amc_website_schemes():
        if _is_junk_scheme_name(code, "amc_website"):
            yield amc, source, as_of, payload, code


def _load_amfi_schemes() -> Iterable[tuple[str, str, str, dict]]:
    """Yield (amc, source, as_of, scheme_payload) from AMFI / mfdata-injected
    monthly-disclosure JSON files under data/parsed/amfi/.

    Schema per file: {amc, as_of, schemes: {fund_name: {holdings: [
        {company, isin, percent_nav, market_value, sector, section} ]}}}.
    This is the highest-quality %NAV source (standard SEBI/AMFI format).
    """
    if not AMFI_DIR.is_dir():
        return
    for p in sorted(AMFI_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        amc = (doc.get("amc") or p.stem).strip()
        as_of = doc.get("as_of") or ""
        schemes = doc.get("schemes")
        if not isinstance(schemes, dict):
            continue
        for fund, payload in schemes.items():
            if not isinstance(payload, dict) or not payload.get("holdings"):
                continue
            if _is_junk_scheme_name(fund, "amfi"):
                continue
            yield amc, "amfi", as_of, {"fund_name": fund, "date": as_of,
                                       "holdings": payload["holdings"]}


def _load_advisorkhoj_schemes() -> Iterable[tuple[str, str, str, dict]]:
    """Yield (amc, source, as_of, scheme_payload) from advisorkhoj JSONs.

    Structure: {amc, files: [{sheets: [{scheme, date, plans: {plan: {holdings}}}]}]}.
    Holdings across plans are merged into one scheme payload.
    """
    if not ADVISORKHOJ_DIR.is_dir():
        return
    for p in sorted(ADVISORKHOJ_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        amc = doc.get("amc") or p.stem
        for f in doc.get("files") or []:
            for sheet in f.get("sheets") or []:
                scheme_name = sheet.get("scheme")
                if not scheme_name:
                    continue
                if _is_junk_scheme_name(scheme_name, "advisorkhoj"):
                    continue
                merged = {}
                for plan in (sheet.get("plans") or {}).values():
                    for h in plan.get("holdings") or []:
                        merged.setdefault(h.get("isin") or h.get("name"), h)
                holdings = []
                for h in merged.values():
                    holdings.append({
                        "company": h.get("name") or "",
                        "isin": h.get("isin") or "",
                        "sector": h.get("industry") or h.get("rating") or "",
                        "quantity": h.get("quantity") or "",
                        "market_value": h.get("value") or "",
                        "percent_nav": h.get("pct_nav") or "",
                        "yield": h.get("yield") or "",
                        "section": h.get("section") or "",
                    })
                payload = {
                    "scheme_name": scheme_name,
                    "fund_name": scheme_name,
                    "date": sheet.get("date"),
                    "holdings": holdings,
                }
                yield amc, "advisorkhoj", sheet.get("date"), payload


def _load_nifty_weights() -> dict[str, dict[str, float]]:
    """Load optional benchmark weights for index/ETF funds.

    Returns { index_name: { isin: weight_pct } } from data/nifty/weights.json
    if present. Source: niftyindices.com monthly "Market Capitalisation &
    Weightage" report / constituent-weights API.
    """
    if not NIFTY_WEIGHTS_JSON.exists():
        return {}
    try:
        with open(NIFTY_WEIGHTS_JSON, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {}
    out: dict[str, dict[str, float]] = {}
    for idx, items in raw.items():
        if not isinstance(items, dict):
            continue
        w: dict[str, float] = {}
        for k, v in items.items():
            if not isinstance(v, (int, float)):
                continue
            key = k.strip().upper()
            if key.startswith("INE"):
                w[key] = float(v)
            else:  # allow symbol/name keys too, mapped later by ISIN resolution
                w.setdefault("__symbol__:" + key, float(v))
        if w:
            out[idx] = w
    return out


def _load_index_schemes() -> Iterable[tuple[str, str, str, dict]]:
    """Yield (amc, source, as_of, scheme_payload) from index_resolved_holdings.json."""
    if not INDEX_RESOLVED_JSON.exists():
        return
    with open(INDEX_RESOLVED_JSON, encoding="utf-8") as fh:
        doc = json.load(fh)
    for key, payload in doc.items():
        if not isinstance(payload, dict):
            continue
        fund = payload.get("fund") or key.split("|")[-1].strip()
        amc = (payload.get("amc") or key.split("|")[0].strip() or "Index").strip()
        holdings = payload.get("holdings") or []
        scheme = {
            "scheme_name": fund,
            "fund_name": fund,
            "date": payload.get("as_of"),
            "holdings": [{"company": h.get("company", ""), "isin": h.get("isin", "")}
                         for h in holdings],
            "_index": payload.get("index", ""),
        }
        yield amc, "index", payload.get("as_of"), scheme


def _infer_category(fund_name: str) -> str:
    n = norm_name(fund_name)
    kw = {
        "Equity": ["equity", "elss", "index", "etf", "infrastructure", "banking", "psu",
                   "consumption", "flexicap", "midcap", "largecap", "smallcap", "multicap",
                   "value", "growth", "dividend", "momentum", "focussed", "sector"],
        "Debt": ["liquid", "gilt", "bond", "money market", "overnight", "arbitrage",
                 "short duration", "low duration", "medium duration", "long duration",
                 "dynamic bond", "credit risk", "corporate bond", "floating", "treasury"],
        "Hybrid": ["hybrid", "balanced", "asset allocation", "aggressive", "conservative"],
    }
    for cat, kws in kw.items():
        if any(k in n for k in kws):
            return cat
    return "Other"


def build_db(force: bool = False) -> Path:
    """Build (or reuse) the cached SQLite database and return its path.

    ``MF_READONLY_DB=1`` (Railway deploy): the fingerprint check is skipped and
    an existing ``webapp.db`` is used as-is -- the deploy image starts with a
    prebuilt DB, never rebuilds from missing source files, and validations
    that data/ sources are present stay local-dev only.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("MF_READONLY_DB") == "1":
        if DB_PATH.exists():
            return DB_PATH
        remote_store.ensure("webapp.db")
        if DB_PATH.exists():
            return DB_PATH
        print("WARNING: webapp.db missing and no R2 fetch possible", file=sys.stderr)
    fingerprint = _fingerprint()

    if not force and DB_PATH.exists():
        try:
            con = sqlite3.connect(DB_PATH)
            row = con.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
            con.close()
            if row and row[0] == fingerprint:
                return DB_PATH
        except sqlite3.Error:
            pass

    if DB_PATH.exists():
        DB_PATH.unlink()

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    _create_schema(cur)
    _seed_securities(cur)
    _seed_schemes_and_holdings(cur)
    cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('fingerprint', ?)", (fingerprint,))
    con.commit()
    con.close()
    return DB_PATH


def _create_schema(cur: sqlite3.Cursor) -> None:
    cur.executescript(
        """
        CREATE TABLE securities (
            isin TEXT PRIMARY KEY,
            name TEXT,
            aliases TEXT,
            source_count INTEGER,
            confirmed_equity REAL,
            cap TEXT,
            sector TEXT
        );
        CREATE TABLE schemes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            amc TEXT,
            fund_name TEXT,
            source TEXT,
            as_of TEXT,
            category TEXT,
            plan TEXT,
            nav REAL, ter REAL, ter_regular REAL, ter_direct REAL, aum REAL,
            amfi_regular TEXT, amfi_direct TEXT, isin_regular TEXT, isin_direct TEXT,
            ytm REAL, duration REAL, avg_maturity REAL,
            is_index INTEGER, is_etf INTEGER, is_fof INTEGER,
            coverage TEXT,
            n_holdings INTEGER,
            n_equity INTEGER,
            n_debt INTEGER,
            top_holding TEXT,
            top_holding_pct REAL,
            cash_pct REAL,
            large_pct REAL, mid_pct REAL, small_pct REAL,
            index_name TEXT
        );
        CREATE TABLE holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheme_id INTEGER,
            amc TEXT,
            fund_name TEXT,
            company TEXT,
            isin TEXT,
            quantity TEXT,
            market_value REAL,
            percent_nav REAL,
            yield TEXT,
            sector TEXT,
            section TEXT,
            asset_class TEXT,
            source TEXT,
            as_of TEXT
        );
        CREATE INDEX idx_holdings_scheme ON holdings(scheme_id);
        CREATE INDEX idx_holdings_isin ON holdings(isin);
        CREATE INDEX idx_holdings_company ON holdings(company);
        CREATE INDEX idx_holdings_sector ON holdings(sector);
        CREATE INDEX idx_schemes_amc ON schemes(amc);
        CREATE INDEX idx_schemes_category ON schemes(category);
        CREATE INDEX idx_schemes_coverage ON schemes(coverage);
        """
    )


def _seed_securities(cur: sqlite3.Cursor) -> None:
    if not EQUITY_ISINS_CSV.exists():
        return
    rows = []
    with open(EQUITY_ISINS_CSV, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            ce = _num(r.get("confirmed_equity"))
            rows.append((
                (r.get("isin") or "").strip(),
                (r.get("name") or "").strip(),
                (r.get("name_aliases") or "").strip(),
                int(r.get("source_count") or 0) if _num(r.get("source_count")) else 0,
                ce if ce is not None else 0.0,
                (r.get("cap") or "na").strip() or "na",
                (r.get("sector") or "na").strip() or "na",
            ))
    cur.executemany(
        "INSERT OR REPLACE INTO securities VALUES (?,?,?,?,?,?,?)", rows)


def _norm_amfi_code(raw) -> str:
    """Normalize an AMFI scheme code (universe CSV stores them as '154477.0')."""
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        return str(int(float(s)))
    except ValueError:
        return s


def _load_universe_index() -> dict[str, list[dict]]:
    """Map normalized fund-name -> universe rows (for enrichment)."""
    index: dict[str, list[dict]] = {}
    if not UNIVERSE_CSV.exists():
        return index
    with open(UNIVERSE_CSV, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            fund = (r.get("Fund Name") or "").strip()
            if not fund:
                continue
            row = {
                "fund": fund,
                "amfi_code": _norm_amfi_code(r.get("Amficode")),
                "as_of": _as_of_date(r.get("Data as of")),
                "nav": _num(r.get("NAV")),
                "ter": _num(r.get("TER")),
                "aum": _num(r.get("AUM")),
                "category": (r.get("Category") or "").strip(),
                "plan": (r.get("Type") or "").strip(),
                "ytm": _num(r.get("YTM")),
                "duration": _num(r.get("Duration")),
                "avg_maturity": _num(r.get("Av. Mat.")),
                "large_pct": _num(r.get("LargeCap %")),
                "mid_pct": _num(r.get("MidCap %")),
                "small_pct": _num(r.get("SmallCap%")),
            }
            index.setdefault(norm_name(fund), []).append(row)
    return index


def _match_universe(norm_fund: str, uni: dict[str, list[dict]]) -> dict | None:
    if not norm_fund:
        return None
    exact = uni.get(norm_fund)
    if exact:
        return _pick_regular(exact)
    # substring containment on both sides, requiring the shorter side to be
    # a meaningful chunk of the longer (avoids 2-char codes matching any name)
    candidates = []
    for key, rows in uni.items():
        if not key or len(key) < 6:
            continue
        shorter = norm_fund if len(norm_fund) < len(key) else key
        longer = key if len(norm_fund) < len(key) else norm_fund
        if shorter and shorter in longer and len(shorter) >= max(8, int(len(longer) * 0.55)):
            candidates.extend(rows)
    if candidates:
        return _pick_regular(candidates)
    # leading-token prefix match
    for key, rows in uni.items():
        if len(key) >= 12 and key[:12] == norm_fund[:12]:
            candidates.extend(rows)
    if candidates:
        return _pick_regular(candidates)
    return None


def _pick_regular(rows: list[dict]) -> dict:
    regs = [r for r in rows if (r.get("plan") or "").lower() == "regular"]
    if regs:
        return max(regs, key=lambda r: r["aum"] or 0)
    return max(rows, key=lambda r: r["aum"] or 0)


def _containment_ratio(a: str, b: str) -> float:
    """Similarity when one normalized key is a substring of the other.

    Parsed scheme names are noisy (document titles, descriptive parentheticals),
    so the clean universe name is often a substring of them. Returns 0 when the
    containment is too weak to be trustworthy.
    """
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long and len(short) >= 8 and len(short) / len(long) >= 0.25:
        return len(short) / len(long)
    return 0.0


def _coverage_statuses() -> dict[str, dict]:
    statuses: dict[str, dict] = {}
    for path in (DISCOVERY_NEEDED_CSV, NO_DISCLOSURE_CSV):
        if not path.exists():
            continue
        tag = "discovery_needed" if path.name.startswith("discovery") else "no_disclosure"
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                fund = (r.get("fund") or "").strip()
                amc = (r.get("amc") or "").strip()
                if not fund:
                    continue
                entry = {"coverage": tag, "amc": amc, "fund": fund}
                # index by AMC+fund AND by fund alone (AMC spelling varies per source)
                statuses[norm_name(amc + " " + fund)] = entry
                statuses.setdefault(norm_name(fund), entry)
    return statuses


def _navall_canon(name: str) -> str:
    """Canonical key for an AMFI navall scheme name (strips 'Direct Plan - Growth').

    Uses the same robust fund-name stripping as the AMFI factsheet splitter
    (``src.amfi_nav.fund_name_from_nav``) so dash-attached variants such as
    'X FUND-DIRECT-GROWTH' normalise to the fund-level name.
    """
    return _navall_fund_key(name)


# Trailing disclosure-date suffix that AMC portfolio sites append to fund names
# (e.g. 'HSBC FLEXI CAP FUND 31 JUL 2026').
_TRAILING_DATE_RE = re.compile(
    r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\s*$",
    re.IGNORECASE)


def _clean_scheme_text(name: str) -> str:
    """Strip document-level noise from a scheme name before keying.

    Handles newline-embedded scheme descriptions (Capitalmind amc-site parses),
    trailing descriptive parentheticals, trailing disclosure-date suffixes, the
    'Portfolio of X as on <date>' wrapper (Kotak advisorkhoj parses), leading
    exchange/IB sheet-code prefixes and HTML-escaped apostrophes.
    """
    s = (name or "").split("\n")[0].strip()
    s = re.sub(r"\s*\([^)]{15,}\)\s*$", "", s).strip()   # trailing description
    s = _TRAILING_DATE_RE.sub("", s).rstrip(" -–—:·")
    # 'Portfolio of Kotak Nifty 50 ETF as on 31-Jul-2026' -> 'Kotak Nifty 50 ETF'
    m = re.match(r"^\s*Portfolio\s+of\s+(.+?)(?:\s+as\s+on\s+.*)?$", s, re.I)
    if m:
        s = m.group(1).strip()
    # leading exchange / sheet-code prefix e.g. 'IB21-Groww Nifty ...' -> 'Groww Nifty ...'
    s = re.sub(r"^[A-Za-z]*\d+[-–—]\s*", "", s).strip()
    # HTML/numeric apostrophe entity: 'UTI CHILDREN 039 S' -> "UTI CHILDREN'S"
    s = re.sub(r"\s0?39\s", "'", s, flags=re.I)
    return s.strip()


def _navall_fund_key(name: str) -> str:
    """Fund-level canonical key for matching scheme names against NAVAll.

    Applies ``&`` -> 'and' (NAVAll uses '&', scheme parses use 'and'),
    plan/option stripping, then brand-prefix normalisation.
    """
    raw = _clean_scheme_text(name).replace("&", " and ")
    stripped = fund_name_from_nav(raw)
    # NAVAll also writes single-series plans as a bare trailing option word with
    # no leading dash (e.g. 'Kotak Gold Fund Growth'). Drop a trailing bare
    # plan/option token so 'Kotak Gold Fund Growth' == 'Kotak Gold Fund'.
    toks = re.split(r"[^a-z0-9]+", stripped.lower())
    while toks and toks[-1] in _BARE_PLAN_TOKENS:
        toks.pop()
    n = norm_name(" ".join(toks))
    for prefix, canon in sorted(_BRAND_PREFIXES.items(), key=lambda kv: -len(kv[0])):
        if n.startswith(prefix):
            return canon + n[len(prefix):]
    return n


_BARE_PLAN_TOKENS = {
    "growth", "idcw", "idcwm", "idcwn", "dividend", "div", "bonus", "payout",
    "reinvestment", "reinvest", "monthly", "weekly", "daily", "quarterly",
    "half", "annual", "option", "plan",
}


def _load_navall_plan_codes() -> dict[str, dict]:
    """Map canon fund-name -> {amfi_regular, amfi_direct, isin_regular, isin_direct}
    from AMFI navall.txt (Scheme Code; ISIN Growth; ISIN Div Reinv; Scheme Name; NAV; Date).

    Regular/Direct rows are keyed by plan; growth rows are preferred for the scheme
    ISIN. Single-series schemes (ETFs, gold/silver funds, etc.) carry no
    regular/direct token — their code is stored under BOTH plans so the NAV chart
    renders regardless of the selected tab.
    """
    path = DATA_DIR / "universe" / "navall.txt"
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("Scheme Code"):
                continue
            parts = line.split(";")
            if len(parts) < 6:
                continue
            code = parts[0].strip()
            isin_g = parts[1].strip()
            name = parts[3].strip()
            low = name.lower()
            if not code or code.lower() == "scheme code":
                continue
            if "direct" in low:
                plan = "direct"
            elif "regular" in low:
                plan = "regular"
            else:
                plan = None
            is_growth = "growth" in low
            key = _navall_canon(name)
            if not key:
                continue
            entry = out.setdefault(key, {"amfi_regular": None, "amfi_direct": None,
                                         "isin_regular": None, "isin_direct": None})
            if plan is None:
                # Single-series scheme (ETF / gold / silver / liquid etc.)
                if entry["amfi_regular"] is None:
                    entry["amfi_regular"] = code
                    entry["amfi_direct"] = code
                if isin_g and isin_g != "-" and len(isin_g) >= 6:
                    entry["isin_regular"] = isin_g
                    entry["isin_direct"] = isin_g
                continue
            if is_growth or entry[f"amfi_{plan}"] is None:
                entry[f"amfi_{plan}"] = code
                if isin_g and isin_g != "-" and len(isin_g) >= 6:
                    entry[f"isin_{plan}"] = isin_g
    return out


# Data-source priority for the holdings snapshot shown per scheme. Lower number
# wins. AMFI (official SEBI/AMFI monthly disclosure) is authoritative, then the
# individual AMC websites' own monthly portfolio, then advisorkhoj (a third-party
# republisher), then index/benchmark-resolved holdings. Within the same source,
# the snapshot with the best %NAV coverage still wins.
_SOURCE_PRIORITY = {
    "amfi": 0,
    "amc_website": 1,
    "advisorkhoj": 2,
    "index": 3,
}


def _seed_schemes_and_holdings(cur: sqlite3.Cursor) -> None:
    uni = _load_universe_index()
    coverage = _coverage_statuses()
    nifty_weights = _load_nifty_weights()
    navall_codes = _load_navall_plan_codes()

    # plan-stripped fund index so plan-level universe rows link to fund-level schemes
    uni_stripped: dict[str, list[dict]] = {}
    for rows in uni.values():
        for r in rows:
            uni_stripped.setdefault(canon_name(r.get("fund", "")), []).append(r)

    scheme_rows: dict[str, dict] = {}
    holding_buf: dict[int, dict] = {}   # sid -> {source -> {security_key: agg_row}}
    scheme_slots = {}
    # sid -> matched universe row (for Amficode fallback when navall is missing)
    universe_match_by_key: dict[int, dict] = {}

    def ensure_scheme(amc: str, fund: str, source: str, as_of: str, payload: dict) -> int:
        key = norm_name(f"{amc} {fund}")
        if key in scheme_slots:
            return scheme_slots[key]
        uni_match = (_match_universe(key, uni)
                     or _match_universe(norm_name(fund), uni)
                     or _match_universe(canon_name(fund), uni_stripped))
        cov = coverage.get(key, {}) or coverage.get(norm_name(fund)) or {}
        idx = payload.get("_index", "")
        is_index = 1 if (source == "index" or "index" in (fund or "").lower() or idx) else 0
        is_etf = 1 if (" etf" in (fund or "").lower() or fund.upper().endswith("ETF")) else 0
        is_fof = 1 if "fof" in (fund or "").lower() else 0
        category = (uni_match or {}).get("category") or _infer_category(fund)

        plan = (uni_match or {}).get("plan") or "Regular"
        sid = len(scheme_slots) + 1
        universe_match_by_key[sid] = uni_match or {}
        scheme_rows[key] = {
            "id": sid, "key": key, "amc": amc, "fund_name": fund, "source": source,
            "as_of": _clean_holdings_date(as_of or (uni_match or {}).get("as_of") or ""),
            "category": category, "plan": plan,
            "nav": (uni_match or {}).get("nav"), "ter": (uni_match or {}).get("ter"),
            "ter_regular": _ter_by_plan(uni_match, "regular"),
            "ter_direct": _ter_by_plan(uni_match, "direct"),
            "amfi_regular": None, "amfi_direct": None,
            "isin_regular": None, "isin_direct": None,
            "aum": (uni_match or {}).get("aum"), "ytm": (uni_match or {}).get("ytm"),
            "duration": (uni_match or {}).get("duration"),
            "avg_maturity": (uni_match or {}).get("avg_maturity"),
            "is_index": is_index, "is_etf": is_etf, "is_fof": is_fof,
            "coverage": cov.get("coverage", "has_holdings"),
            "n_holdings": 0, "n_equity": 0, "n_debt": 0,
            "top_holding": "", "top_holding_pct": None, "cash_pct": None,
            "large_pct": (uni_match or {}).get("large_pct"),
            "mid_pct": (uni_match or {}).get("mid_pct"),
            "small_pct": (uni_match or {}).get("small_pct"),
            "index_name": idx or "",
        }
        scheme_slots[key] = sid
        holding_buf[sid] = {}
        return sid

    # AMFI / mfdata monthly disclosure (highest-quality %NAV) — loaded first so it
    # is the preferred weighted source when present.
    for amc, source, as_of, payload in _load_amfi_schemes():
        fund = payload.get("fund_name") or ""
        sid = ensure_scheme(amc, fund, source, as_of, payload)
        _insert_holdings(holding_buf[sid].setdefault(source, {}), sid, amc, fund,
                         payload.get("holdings") or [], source, as_of)

    # AMC website parses (clean scheme-indexed payloads; skip sheet-code-KEYED
    # schemes — e.g. PPFAS 'PPFCF' — they are resolved to real funds later by
    # the ISIN-overlap pass)
    for amc, source, as_of, payload, fund, code in _load_amc_website_schemes():
        if _is_junk_scheme_name(code, "amc_website"):
            continue
        sid = ensure_scheme(amc, fund, source, as_of, payload)
        _insert_holdings(holding_buf[sid].setdefault(source, {}), sid, amc, fund,
                         payload.get("holdings") or [], source, as_of)

    # Advisorkhoj parses
    for amc, source, as_of, payload in _load_advisorkhoj_schemes():
        fund = payload.get("fund_name") or payload.get("scheme_name") or ""
        sid = ensure_scheme(amc, fund, source, as_of, payload)
        _insert_holdings(holding_buf[sid].setdefault(source, {}), sid, amc, fund,
                         payload.get("holdings") or [], source, as_of)

    # Index-resolved schemes
    for amc, source, as_of, payload in _load_index_schemes():
        fund = payload.get("fund_name") or payload.get("scheme_name") or ""
        sid = ensure_scheme(amc, fund, source, as_of, payload)
        _insert_holdings(holding_buf[sid].setdefault(source, {}), sid, amc, fund,
                         payload.get("holdings") or [], source, as_of)

    # ---- Resolve sheet-code schemes (e.g. PPFAS 'PPFCF') to real funds ----
    # AMC-site monthly portfolios often key schemes by a code; the real fund name
    # comes from advisorkhoj/the factsheet. Match by ISIN-overlap with real-named
    # schemes of the SAME AMC, then attach the weighted code snapshot to that fund.
    # Dedupe codes that appear in several files (combined + per-scheme) keeping the
    # most complete snapshot, so weights aren't double-counted.
    code_schemes: dict[tuple[str, str], tuple] = {}
    for amc, source, as_of, payload, code in _load_amc_website_code_schemes():
        hs = [h for h in (payload.get("holdings") or []) if isinstance(h, dict) and _looks_like_holding(h)]
        key = (norm_name(amc), code)
        prev = code_schemes.get(key)
        if prev is None or len(hs) > len(prev[3].get("holdings") or []):
            code_schemes[key] = (amc, source, as_of, payload, code, hs)

    real_isins: dict[int, set] = {}
    for sid, src_map in holding_buf.items():
        isins: set[str] = set()
        for srows in src_map.values():
            for a in srows.values():
                if a.get("isin"):
                    isins.add(a["isin"].strip().upper())
        real_isins[sid] = isins
    amc_sids: dict[str, list[int]] = {}
    for srow in scheme_rows.values():
        amc_sids.setdefault(norm_name(srow["amc"]), []).append(srow["id"])

    code_resolved = 0
    for (amc_norm, code), (amc, source, as_of, payload, code_name, clean_hs) in code_schemes.items():
        code_isins = {h.get("isin", "").strip().upper() for h in clean_hs if h.get("isin")}
        if not code_isins:
            continue
        best_sid, best_score = None, 0.0
        for sid in amc_sids.get(amc_norm, []):
            real = real_isins.get(sid, set())
            if not real:
                continue
            common = len(code_isins & real)
            if common < 5:
                continue
            score = common / max(1, len(code_isins))
            if score > best_score:
                best_sid, best_score = sid, score
        if best_sid is None or best_score < 0.5:
            continue
        srow = next(s for s in scheme_rows.values() if s["id"] == best_sid)
        # attach the weighted code snapshot to the real fund
        _insert_holdings(holding_buf[best_sid].setdefault("amc_website", {}),
                         best_sid, srow["amc"], srow["fund_name"],
                         clean_hs, "amc_website", as_of)
        real_isins[best_sid] |= code_isins
        code_resolved += 1

    # Link plan-level universe rows onto existing fund-level schemes; only create
    # a scheme for funds genuinely absent from holdings (true missing coverage).
    # Reverse index: canonical fund name -> existing scheme ids
    fund_index: dict[str, list[int]] = {}
    for srow in scheme_rows.values():
        fund_index.setdefault(canon_name(srow["fund_name"]), []).append(srow["id"])
    fund_index_amc: dict[tuple, list[int]] = {}
    for srow in scheme_rows.values():
        fund_index_amc.setdefault(
            (canon_name(srow["fund_name"]), norm_name(srow["amc"])), []).append(srow["id"])

    def merge_universe(srow: dict, r: dict) -> None:
        """Enrich an existing scheme with a universe row's attributes."""
        if srow.get("aum") is None:
            srow["aum"] = r.get("aum")
        if srow.get("nav") is None:
            srow["nav"] = r.get("nav")
        if srow.get("ter") is None:
            srow["ter"] = r.get("ter")
        if srow.get("ytm") is None:
            srow["ytm"] = r.get("ytm")
        if srow.get("duration") is None:
            srow["duration"] = r.get("duration")
        if srow.get("avg_maturity") is None:
            srow["avg_maturity"] = r.get("avg_maturity")
        if not srow.get("category") or srow["category"] == "Other":
            srow["category"] = r.get("category") or srow.get("category")
        if not srow.get("as_of"):
            srow["as_of"] = _clean_holdings_date(r.get("as_of") or "")
        plan = (r.get("plan") or "").lower()
        if plan == "regular":
            srow["plan"] = "Regular"
            if r.get("ter") is not None:
                srow["ter_regular"] = r.get("ter")
                srow["ter"] = r.get("ter")
        elif plan == "direct":
            if r.get("ter") is not None:
                srow["ter_direct"] = r.get("ter")

    # track which universe rows we've merged to avoid re-merging duplicates
    merged_universe: set[int] = set()
    uni_only_by_fk: dict[str, int] = {}

    def find_target_cands(fk: str, amc_fk: str) -> list[int]:
        """Locate existing schemes that correspond to a universe fund name."""
        if not fk or len(fk) < 6:
            return []
        cands = fund_index_amc.get((fk, amc_fk), [])
        if cands:
            return cands
        cands = fund_index.get(fk, [])
        if cands:
            return cands
        # fuzzy containment: parsed names are noisy (document titles, descriptive
        # parentheticals), so the clean universe name is often a substring of them.
        best, best_ratio = [], 0.0
        for key, sids in fund_index_amc.items():
            if key[1] != amc_fk:
                continue
            r = _containment_ratio(fk, key[0])
            if r > best_ratio:
                best, best_ratio = sids, r
        if best:
            return best
        for key, sids in fund_index.items():
            r = _containment_ratio(fk, key)
            if r > best_ratio:
                best, best_ratio = sids, r
        return best

    for rows in uni.values():
        for r in rows:
            fund = r.get("fund", "")
            if _is_junk_scheme_name(fund, "universe"):
                continue
            fk = canon_name(fund)
            amc_hint = _amc_from_fund(fund)
            amc_fk = norm_name(amc_hint)
            cands = find_target_cands(fk, amc_fk)
            if cands:
                target = None
                for sid in cands:
                    srow = next(s for s in scheme_rows.values() if s["id"] == sid)
                    if not holding_buf.get(sid):
                        continue
                    if target is None or (r.get("aum") or 0) > (target.get("aum") or 0):
                        target = srow
                if target is None:
                    # no candidate has holdings; fall back to any candidate
                    target = next(s for s in scheme_rows.values() if s["id"] == cands[0])
                merge_universe(target, r)
                merged_universe.add(target["id"])
                continue
            # genuinely not in holdings -> create a missing-coverage scheme
            key = norm_name(f"{amc_hint} {fund}")
            cov = coverage.get(key) or coverage.get(norm_name(fund)) or {}
            # reuse an earlier universe-only scheme for the same fund (other plan rows)
            if fk in uni_only_by_fk:
                srow = next(s for s in scheme_rows.values() if s["id"] == uni_only_by_fk[fk])
                merge_universe(srow, r)
                if not srow["n_holdings"]:
                    srow["coverage"] = cov.get("coverage", "no_disclosure")
                srow["amc"] = cov.get("amc") or srow["amc"]
                continue
            if key in scheme_slots:
                continue
            ensure_scheme(amc_hint, fund, "universe_only", r.get("as_of", ""), {
                "holdings": [], "fund_name": fund,
            })
            if key in scheme_rows:
                srow = scheme_rows[key]
                if not srow["n_holdings"]:
                    srow["coverage"] = cov.get("coverage", "no_disclosure")
                srow["amc"] = cov.get("amc") or srow["amc"]
                uni_only_by_fk[fk] = srow["id"]

    # Attach AMFI scheme codes + scheme ISINs for Regular & Direct plans (navall).
    # Universe-CSV Amficodes (from the matched universe row) are a fallback when
    # the fund is absent from navall.txt but present in the curated universe.
    for srow in scheme_rows.values():
        um = universe_match_by_key.get(srow["id"])
        if um and um.get("amfi_code"):
            if (um.get("plan") or "").lower() == "direct":
                srow["amfi_direct"] = srow["amfi_direct"] or um["amfi_code"]
            else:
                srow["amfi_regular"] = srow["amfi_regular"] or um["amfi_code"]
        nc = navall_codes.get(_navall_fund_key(srow["fund_name"]))
        if nc:
            srow["amfi_regular"] = nc["amfi_regular"] or srow.get("amfi_regular")
            srow["amfi_direct"] = nc["amfi_direct"] or srow.get("amfi_direct")
            srow["isin_regular"] = nc["isin_regular"] or srow.get("isin_regular")
            srow["isin_direct"] = nc["isin_direct"] or srow.get("isin_direct")

    # Finalize per-scheme stats from the best (most complete) source snapshot
    holding_rows: list[tuple] = []
    for sid, src_map in holding_buf.items():
        key = next(k for k, s in scheme_rows.items() if s["id"] == sid)
        srow = scheme_rows[key]
        if not src_map:
            continue
        # Prefer the source snapshot by DATA-SOURCE PRIORITY (AMFI > AMC website >
        # advisorkhoj > index), then by WEIGHT coverage within the same source —
        # the primary outcome is the %NAV breakup (overlap depends on it):
        #  1) highest fraction of rows carrying percent_nav
        #  2) most rows with percent_nav, 3) most ISIN coverage, 4) most rows.
        # A snapshot whose weights FAIL validation (see guard below) is demoted
        # by +2 priority so e.g. noisy OCR parses never displace clean
        # advisorkhoj data purely on source rank.
        def _weight_valid(kv):
            rws = list(kv[1].values())
            pcts = [a["percent_nav"] for a in rws if a["percent_nav"] is not None]
            if not pcts:
                return True  # nothing to invalidate
            return max(pcts) <= 100 and sum(pcts) <= 120

        def _src_score(kv):
            rows = list(kv[1].values())
            n = max(len(rows), 1)
            with_pct = sum(1 for a in rows if a["percent_nav"] is not None)
            isin = sum(1 for a in rows if a["isin"])
            prio = _SOURCE_PRIORITY.get(kv[0], 9) + (0 if _weight_valid(kv) else 2)
            return (-prio, with_pct / n, with_pct, isin, len(rows))
        best_src = max(src_map.items(), key=_src_score)
        agg = list(best_src[1].values())
        srow["source"] = best_src[0]
        if srow["as_of"] != "universe_only" and not srow["as_of"]:
            srow["as_of"] = next((a["as_of"] for a in agg if a["as_of"]), "")
            srow["as_of"] = _clean_holdings_date(srow["as_of"])

        # VALIDATE source weights: a holding weight can never exceed 100% and a
        # scheme's weights can't sum to wildly more than 100% (arbitrage/leverage
        # may reach ~150-200, but thousands indicate a corrupt scale). Some
        # advisorkhoj index funds leak quantities/points into %NAV — treat such a
        # source as unweighted so the mv / equal-weight fallbacks rebuild weights.
        # Threshold >120%: even heavily leveraged funds rarely exceed ~120-130%,
        # so a higher total indicates a corrupt scale (advisorkhoj ~2-3x inflation).
        src_pcts = [a["percent_nav"] for a in agg if a["percent_nav"] is not None]
        src_max = max(src_pcts, default=0.0)
        src_sum = sum(src_pcts)
        if src_max > 100 or src_sum > 120:
            for a in agg:
                a["percent_nav"] = None

        # %NAV fallback: where a holding lacks a weight but has a market value,
        # compute pct = market_value / sum(market_value) (scale-invariant).
        missing_pct = [a for a in agg if a["percent_nav"] is None]

        # Benchmark weights for index/ETF funds: if the scheme tracks a Nifty
        # index for which weights were ingested, use the actual index weights.
        if srow.get("is_index") or srow.get("source") == "index":
            idx_key = srow.get("index_name") or ""
            weights = nifty_weights.get(idx_key) or nifty_weights.get(idx_key.upper())
            if weights and missing_pct:
                for a in missing_pct:
                    if not a["isin"]:
                        continue
                    wgt = weights.get(a["isin"].upper())
                    if wgt is not None:
                        a["percent_nav"] = round(wgt, 6)

        if missing_pct:
            total_mv = sum(a["market_value"] for a in agg if a["market_value"] is not None)
            if total_mv:
                for a in missing_pct:
                    if a["percent_nav"] is None and a["market_value"] is not None:
                        a["percent_nav"] = round(a["market_value"] / total_mv * 100, 6)

        # Equal-weight fallback for schemes (index/ETF/weightless) that still
        # have no weights at all — so detail and overlap both show a breakup.
        if not any(a["percent_nav"] is not None for a in agg) and agg:
            w = 100.0 / len(agg)
            for a in agg:
                a["percent_nav"] = round(w, 6)

        # FINAL guard: no weight may exceed 100%, none may be deeply negative,
        # and the scheme can't sum to more than 120% (inflated advisorkhoj scales).
        # If any invariant is violated after all fallbacks, fall back to equal weight.
        final_pcts = [a["percent_nav"] for a in agg if a["percent_nav"] is not None]
        if (final_pcts and
                (max(final_pcts) > 100 or sum(final_pcts) > 120 or any(p < -5 for p in final_pcts))):
            w = 100.0 / len(agg)
            for a in agg:
                a["percent_nav"] = round(w, 6)

        pcts = [a["percent_nav"] for a in agg if a["percent_nav"] is not None]
        srow["n_holdings"] = len(agg)
        srow["n_equity"] = sum(1 for a in agg if _section_is_equity(a["section"]))
        srow["n_debt"] = srow["n_holdings"] - srow["n_equity"]
        if pcts:
            top = max(agg, key=lambda a: a["percent_nav"] or 0)
            srow["top_holding"] = top["company"]
            srow["top_holding_pct"] = top["percent_nav"]
            srow["cash_pct"] = round(sum(p for p in pcts if p <= 0), 4) or None
        holding_rows.extend(
            (a["sid"], a["amc"], a["fund"], a["company"], a["isin"], a["quantity"],
             a["market_value"], a["percent_nav"], a["yield"], a["sector"], a["section"],
             a["asset_class"], a["source"], a["as_of"])
            for a in agg)

    cur.executemany(
        """INSERT OR REPLACE INTO schemes (
            id, key, amc, fund_name, source, as_of, category, plan, nav, ter,
            ter_regular, ter_direct, aum, amfi_regular, amfi_direct,
            isin_regular, isin_direct,
            ytm, duration, avg_maturity, is_index, is_etf, is_fof, coverage,
            n_holdings, n_equity, n_debt, top_holding, top_holding_pct, cash_pct,
            large_pct, mid_pct, small_pct, index_name
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [_row_tuple(scheme_rows[key]) for key in scheme_rows],
    )

    cur.executemany(
        """INSERT INTO holdings (
            scheme_id, amc, fund_name, company, isin, quantity, market_value,
            percent_nav, yield, sector, section, asset_class, source, as_of
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        holding_rows,
    )


def _row_tuple(r: dict) -> tuple:
    return (
        r["id"], r["key"], r["amc"], r["fund_name"], r["source"], r["as_of"],
        r["category"], r["plan"], r["nav"], r["ter"],
        r.get("ter_regular"), r.get("ter_direct"), r["aum"],
        r.get("amfi_regular"), r.get("amfi_direct"), r.get("isin_regular"), r.get("isin_direct"),
        r["ytm"],
        r["duration"], r["avg_maturity"], r["is_index"], r["is_etf"], r["is_fof"],
        r["coverage"], r["n_holdings"], r["n_equity"], r["n_debt"], r["top_holding"],
        r["top_holding_pct"], r["cash_pct"], r["large_pct"], r["mid_pct"],
        r["small_pct"], r["index_name"],
    )


def _ter_by_plan(uni_match: dict | None, plan: str) -> float | None:
    """TER for a specific plan from a single matched universe row."""
    if not uni_match:
        return None
    if (uni_match.get("plan") or "").lower() == plan:
        return uni_match.get("ter")
    return None


def _section_is_equity(section: str) -> bool:
    s = (section or "").lower()
    return "debt" not in s and "cash" not in s and "net" not in s and "receivable" not in s


_ASSET_LABELS = {
    "stocks": "Stocks",
    "debt": "Debt",
    "international": "International",
    "future_options": "Futures & Options",
    "cash_equivalents": "Cash equivalents",
    "other": "Other",
}

_INTL_ISIN_PREFIXES = ("US", "LU", "DE", "GB", "JP", "HK", "SG", "EU", "NL", "FR",
                       "IE", "CH", "CA", "AU", "NO", "SE", "DK", "FI")


def classify_asset(section: str, company: str, isin: str) -> str:
    """Bucket a holding into Stocks / Debt / International / Futures & Options /
    Cash equivalents ('' when unknown — refined at query time with security tags)."""
    section = (section or "").lower()
    company = (company or "").lower()
    isin = (isin or "").upper()

    # Futures / Options / derivatives (also derivative expiry codes like 'AUG26')
    if re.match(r"^[A-Z]{3}\d{2}$", isin) \
            or any(k in section for k in ("derivative", "futures", "option", "contract", "swap")) \
            or "future" in company or "option" in company:
        return "future_options"
    # Cash equivalents
    if any(k in section for k in ("cash", "treps", "reverse repo", "short term deposit",
                                  "net current assets", "net receivables", "cash & cash",
                                  "bank deposit", "cbfl", "liquid fund", "money at call")):
        return "cash_equivalents"
    # International (foreign securities / ADR / GDR / overseas ETFs)
    if isin.startswith(_INTL_ISIN_PREFIXES) \
            or any(k in section for k in ("foreign", "overseas", "adr", "gdr", "international",
                                          "euro clear", "depositary receipt")):
        return "international"
    # Debt & money market
    if any(k in section for k in ("debt", "bond", "money market", "certificate of deposit",
                                  "commercial paper", "treasury", "g-sec", "gsec", "sdl",
                                  "ncd", "securitised", "government security", "gilt",
                                  "floating rate", "credit risk", "strips", "treps",
                                  "bharat bond", "cp", "cd", "zero coupon")) \
            or any(k in company for k in ("t-bill", "tbill", "treasury", "goi", "g-sec",
                                          "gsec", "gilt", "sdl", "repo", "treps",
                                          "money market", "bank deposit", "debenture",
                                          "ncd", "zero coupon", "commercial paper",
                                          "certificate of deposit")):
        return "debt"
    # Equity / stocks
    if any(k in section for k in ("equity", "stock", "shares", "preference", "warrant")):
        return "stocks"
    return ""


def refine_asset_class(asset_class: str, confirmed_equity, isin: str) -> str:
    """Refine a section-based classification with the securities-directory tag."""
    if asset_class in ("stocks", "debt", "international", "future_options", "cash_equivalents"):
        return asset_class
    isin = (isin or "").upper()
    if re.match(r"^[A-Z]{3}\d{2}$", isin):
        return "future_options"
    if isin.startswith(_INTL_ISIN_PREFIXES):
        return "international"
    if confirmed_equity == 1:
        return "stocks"
    if confirmed_equity == 0.5:
        return "stocks"
    if confirmed_equity == 0:
        return "debt"
    return "other"


_ISIN_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")
_HEADER_FOOTNOTE_RE = re.compile(
    r"^(?:top\s*\d+\s*holdings?|non[- ]\s*sensex|thinly\s+traded|non[- ]\s*traded|"
    r"industry\s+classification|less\s+than\s*0[.,]0?\d*\s*%?|note\b|"
    r"yield\s+to\s+(?:call|maturity|redemption)|net\s+assets|scheme\s+classification|"
    r"as\s+on\b|portfolio\s+summary|fund\s+summary|top\s+holdings?|"
    r"name\s+of\s+(?:the\s+)?(?:instrument|security)|market\s+value\s+of\s+investments?|"
    r"total\s+amount\s+invested|record\s+date|since\s+inception|nil\b|"
    r"returns?\b|annualised|annualized|benchmark\b|trio\b|"
    r"(?:^|\s)\d{1,2}\s*-?\s*(?:year|yr)\b|cagr\b|"
    r"[\*\+\^~£@#\$])\s*", re.IGNORECASE)


_RETURNS_RE = re.compile(
    r"returns?\s*\(|annualis|benchmark\b|since\s+inception|^\d{1,2}\s*[-–]?\s*(?:year|yr)\b|\btrio\b",
    re.IGNORECASE)

# spreadsheet column-header cells that are NOT holdings (slip in from raw tables)
_HEADER_CELLS = {"date", "underlying", "scheme", "series", "issuer", "isin", "name",
                 "quantity", "qty", "market", "value", "market value", "pct", "pct_nav",
                 "percent", "rating", "industry", "sector", "symbol", "security",
                 "instrument", "description", "total", "sub total", "subtotal",
                 "grand total", "particulars", "details", "company", "asset class"}

# footnote / disclosure lines that occupy the section column (not asset classes)
_FOOTNOTE_SECTION_RE = re.compile(
    r"total\s+(number\s+of\s+contracts|exposure|brokerage|investments?|market\s+value|"
    r"value\s+and\s+percentage|outstanding|percentage|dividend|idcw|assets|securities|"
    r"amount|net\s+assets|contracts|transactions)|"
    r"illiquid\s+securities|derivative\s+disclosure|foreign\s+securities|adrs?|gdrs?|"
    r"as\s+on\b|as\s+at\b|exposure\s+due\s+to\s+futures|hedg|repo\s+transactions|"
    r"^total\b|^the\s+total\b|^\(?\d+[).]\s*", re.IGNORECASE)


def _looks_like_holding(h: dict) -> bool:
    """True if a parsed row is a real security holding, not a table header /
    footnote / informative line that slipped into the holdings list.

    Real holdings carry a valid ISIN and a plausible %NAV (a weight can never
    exceed ~1000%, even leveraged). Header/footnote rows (e.g. 'Top Ten
    Holdings', '# Non Sensex Scrips', '~ YTC ...', 'Market value of Investment',
    'Nifty 500 TRI Returns (Annualised)', 'Since Inception ...', or spreadsheet
    column cells like 'Date' / 'Underlying' / 'Series') sometimes carry stray
    numbers in the isin/pct columns, so header/returns NAMES and an implausible
    weight all disqualify the row.
    """
    company = (h.get("company") or h.get("stock_name") or "").strip()
    isin = (h.get("isin") or "").strip()
    section = (h.get("section") or "").strip()

    # Hard rejects — spreadsheet column-header cells and returns/benchmark rows are
    # never real holdings (their isin/company are header tokens, not securities).
    if company.lower() in _HEADER_CELLS or isin.lower() in _HEADER_CELLS:
        return False
    if _HEADER_FOOTNOTE_RE.match(company) or _HEADER_FOOTNOTE_RE.match(isin):
        return False
    if _RETURNS_RE.search(company) or _RETURNS_RE.search(isin):
        return False

    pct = _num(h.get("percent_nav") or h.get("pct_nav"))
    mv = _num(h.get("market_value") or h.get("value"))
    qty = str(h.get("quantity") or "").strip()
    isin_valid = bool(isin and isin.lower() not in ("na", "-", "none") and _ISIN_RE.match(isin))

    # A row with a valid ISIN is a real security — keep it (a plausible weight is
    # not required; section labels like 'Total' must NOT cause a false drop, e.g.
    # international stocks Amazon/Alphabet/Microsoft). Only an absurd weight
    # disqualifies it.
    if isin_valid:
        if pct is not None and abs(pct) > 1000:
            return False
        return True

    # No valid ISIN: must carry plausible numeric data AND not be a footnote row.
    if section and _FOOTNOTE_SECTION_RE.search(section):
        return False
    if pct is not None and abs(pct) <= 1000:
        return True
    if mv is not None:
        return True
    if qty and _num(qty) is not None:
        return True
    return False


def _insert_holdings(rows, sid, amc, fund, holdings, source, as_of) -> None:
    # drop header/footnote/junk rows FIRST (their stray values would otherwise
    # corrupt the fraction-vs-percent scale detection), then normalize weights
    clean = [h for h in holdings if isinstance(h, dict) and _looks_like_holding(h)]
    norm_holdings = _normalize_pct_scale(clean)
    for h in norm_holdings:
        key = (h.get("isin") or "").strip() or (h.get("company") or h.get("stock_name") or "").strip()
        if not key:
            continue
        # Each source contributes its own holdings snapshot for a scheme; the
        # caller merges per-source snapshots and keeps the most complete one
        # (merging %NAV across different as-of dates would be incorrect).
        if key in rows:
            existing = rows[key]
            # within one source, dedupe identical securities
            p = _num(h.get("percent_nav") or h.get("pct_nav"))
            mv = _num(h.get("market_value") or h.get("value"))
            if p is not None:
                existing["percent_nav"] = (existing["percent_nav"] or 0) + p
            if mv is not None:
                existing["market_value"] = (existing["market_value"] or 0) + mv
            continue
        rows[key] = {
            "sid": sid, "amc": amc, "fund": fund,
            "company": (h.get("company") or h.get("stock_name") or "").strip(),
            "isin": (h.get("isin") or "").strip(),
            "quantity": str(h.get("quantity") or ""),
            "market_value": _num(h.get("market_value") or h.get("value")),
            "percent_nav": _num(h.get("percent_nav") or h.get("pct_nav")),
            "yield": str(h.get("yield") or ""),
            "sector": (h.get("sector") or h.get("industry") or h.get("rating") or "").strip(),
            "section": (h.get("section") or "").strip(),
            "asset_class": classify_asset(h.get("section") or "", h.get("company") or h.get("stock_name") or "", h.get("isin") or ""),
            "source": source,
            "as_of": as_of or "",
        }


def _normalize_pct_scale(holdings: list[dict]) -> list[dict]:
    """Advisorkhoj stores %NAV as fractions (0.10 = 10%), AMC-site parses store
    percentages (10.0). Normalize a scheme's holdings to the 0-100 scale."""
    pcts = [_num(h.get("percent_nav") or h.get("pct_nav")) for h in holdings if isinstance(h, dict)]
    pcts = [p for p in pcts if p is not None and p > 0]
    if not pcts:
        return list(holdings)
    mx = max(pcts)
    if 0 < mx < 2:  # looks like a fraction
        out = []
        for h in holdings:
            h = dict(h)
            for k in ("percent_nav", "pct_nav"):
                if h.get(k) is not None:
                    v = _num(h.get(k))
                    h[k] = round(v * 100, 6) if v is not None else h.get(k)
            out.append(h)
        return out
    return list(holdings)


_REGISTRY_FILE = BASE_DIR / "config" / "amc_registry.json"

_AMC_ALIASES = {
    "adityabirlasunlife": "Aditya Birla Sun Life Mutual Fund",
    "adityabirlasl": "Aditya Birla Sun Life Mutual Fund",
    "abs": "Aditya Birla Sun Life Mutual Fund",
    "absl": "Aditya Birla Sun Life Mutual Fund",
    "barodabnp": "Baroda BNP Paribas Mutual Fund",
    "kotakmahindra": "Kotak Mahindra Mutual Fund",
    "kotak": "Kotak Mahindra Mutual Fund",
    "nipponindia": "Nippon India Mutual Fund",
    "nippon": "Nippon India Mutual Fund",
    "mahindramanulife": "Mahindra Manulife Mutual Fund",
    "mahindra": "Mahindra Manulife Mutual Fund",
    "motilaloswal": "Motilal Oswal Mutual Fund",
    "motilal": "Motilal Oswal Mutual Fund",
    "ilfs": "IL&FS Mutual Fund (IDF)",
    "jmfinancial": "JM Financial Mutual Fund",
    "jm": "JM Financial Mutual Fund",
    "whiteoak": "WhiteOak Capital Mutual Fund",
    "360one": "360 ONE Mutual Fund",
    "360": "360 ONE Mutual Fund",
    "icicipru": "ICICI Prudential Mutual Fund",
    "icici": "ICICI Prudential Mutual Fund",
    "canararobeco": "Canara Robeco Mutual Fund",
    "canararob": "Canara Robeco Mutual Fund",
    "tata": "Tata Mutual Fund",
    "sbi": "SBI Mutual Fund",
    "hdfc": "HDFC Mutual Fund",
    "uti": "UTI Mutual Fund",
    "axis": "Axis Mutual Fund",
    "dsp": "DSP Mutual Fund",
    "franklin": "Franklin Templeton Mutual Fund",
    "mirae": "Mirae Asset Mutual Fund",
    "bandhan": "Bandhan Mutual Fund",
    "edelweiss": "Edelweiss Mutual Fund",
    "quant": "Quant Mutual Fund",
    "quantum": "Quantum Mutual Fund",
    "ppfas": "PPFAS Mutual Fund",
    "navi": "Navi Mutual Fund",
    "groww": "Groww Mutual Fund",
    "zerodha": "Zerodha Mutual Fund",
    "union": "Union Mutual Fund",
    "sundaram": "Sundaram Mutual Fund",
    "invesco": "Invesco Mutual Fund",
    "litchfield": "ITI Mutual Fund",
    "iti": "ITI Mutual Fund",
    "bajaj": "Bajaj Finserv Mutual Fund",
    "truemutualfund": "Trust Mutual Fund",
    "trust": "Trust Mutual Fund",
    "lic": "LIC Mutual Fund",
    "oldbridge": "Old Bridge Mutual Fund",
    "angelone": "Angel One Mutual Fund",
    "samco": "Samco Mutual Fund",
    "shriram": "Shriram Mutual Fund",
    "uniabst": "Unifi Mutual Fund",
    "unifi": "Unifi Mutual Fund",
    "thewealthcompany": "The Wealth Company Mutual Fund",
    "helio": "Helios Mutual Fund",
    "helios": "Helios Mutual Fund",
    "choice": "Choice Mutual Fund",
    "abakkus": "Abakkus Mutual Fund",
    "alphagrep": "AlphaGrep Mutual Fund",
    "nj": "NJ Mutual Fund",
    "capitalmind": "Capitalmind Mutual Fund",
    "bansalfinance": "Bajaj Finserv Mutual Fund",
    "hsbc": "HSBC Mutual Fund",
    "jio": "Jio BlackRock Mutual Fund",
    "jioblackrock": "Jio BlackRock Mutual Fund",
    "invesco": "Invesco Mutual Fund",
}


def _load_registry_names() -> list[str]:
    try:
        with open(_REGISTRY_FILE, encoding="utf-8-sig") as fh:
            reg = json.load(fh)
        return [r.get("mf_name", "") for r in reg if r.get("mf_name")]
    except Exception:
        return []


_registry_names: list[str] | None = None


def _amc_from_fund(fund: str) -> str:
    global _registry_names
    if _registry_names is None:
        _registry_names = _load_registry_names()
    n = norm_name(fund)
    if not n:
        return "Universe"
    # prefer longest brand-prefix matches first to avoid substring false positives
    # (e.g. "jm" inside "nj momentum fund")
    for alias, amc in sorted(_AMC_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if n.startswith(alias):
            return amc
    for amc in _registry_names:
        na = norm_name(amc)
        if na and (na in n or n.startswith(na.split("mutualfund")[0])):
            return amc
    return "Universe"


# --------------------------------------------------------------------------
# Query layer
# --------------------------------------------------------------------------

_NAV_MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _nav_date_key(datestr: str) -> str:
    """Normalise a NAV date to a sortable/comparable 'YYYY-MM-DD' key.

    Handles both '18-Aug-2026' (nav_history) and '2026-08-18' (API input).
    Returns '' when unparseable.
    """
    s = (datestr or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", s)
    if m:
        mon = _NAV_MONTHS.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


def _clean_holdings_date(as_of) -> str:
    """Normalise a portfolio-holdings announcement date to clean 'YYYY-MM-DD'.

    Handles the verbose formats found in the parsed data ('Portfolio as on
    31-Jul-2026', 'Monthly Portfolio Statement as of July 31, 2026', 'April 30,
    2026', …) and returns '' for junk values (plan codes like 'IDF255', empty).
    """
    s = (as_of or "").strip()
    if not s:
        return ""
    low = s.lower()
    m = re.search(r"(\d{1,2})\s*[- ]\s*([a-z]{3})[a-z]*[- ](\d{4})", low)
    if m:
        mon = _NAV_MONTHS.get(m.group(2)[:3])
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2})[a-z]*,?\s+(\d{4})", low)
    if m:
        mon = _NAV_MONTHS.get(m.group(1)[:3])
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(2)):02d}"
    return ""


_SCHEME_COLS = ("id", "amc", "fund_name", "source", "as_of", "category", "plan", "nav",
                "ter", "aum", "ytm", "duration", "avg_maturity", "is_index", "is_etf",
                "is_fof", "coverage", "n_holdings", "n_equity", "n_debt", "top_holding",
                "top_holding_pct", "cash_pct", "large_pct", "mid_pct", "small_pct", "index_name")


_COUPON_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%")
_MATURITY_RE = re.compile(r"\((\d{2})/(\d{2})/(\d{4})\)")
_RATING_LIKE_RE = re.compile(r"(?:AAA|AA\+?|A1\+?|A\+?|BBB\+?|BB\+?|B\+?|Sovereign|SOV|Unrated|NR)", re.IGNORECASE)

# Coverage diagnostics for the bond-market data (webapp/db.py. bond_coverage)
_COUPON_PCT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,4})?)\s*%")
_ZERO_FLOATER_RE = re.compile(
    r"(^(?:0|[A-Z]{0,4}\s?0\.\d+)%|\b0%\b|floating|frb|frn|floater|rollover|"
    r"t-?bill\s*lnk|lnk\s+to|linked|strpp|strips)", re.IGNORECASE)
_ZERO_FLOATER_SEGMENTS = frozenset(
    {"T-Bill", "Commercial Paper", "Certificate of Deposit", "G-Sec FRB",
     "G-Sec STRIPS", "STRIPPED Debt"})


def _zero_or_floater(rec: dict) -> bool:
    return ((rec.get("segment") or "") in _ZERO_FLOATER_SEGMENTS
            or bool(_ZERO_FLOATER_RE.search(str(rec.get("name") or ""))))


def _zero_or_floater_name(name: str) -> bool:
    return bool(_ZERO_FLOATER_RE.search(name or ""))


# ---- debt instrument identity for overlap fallback [DBT4] -------------------
# The same bond is often reported differently across sources: one AMC prints
# "7.38% Gujarat SDL (15/11/2032)" with the ISIN, another "Gujarat SDL 7.38
# 15/11/2032" without. Keying overlap by ISIN-or-exact-name alone then
# under-counts debt overlap. This derives an issuer+coupon+maturity identity
# from the holding line itself.
_DEBT_KEY_NOISE_RE = re.compile(r"[^A-Z0-9 ]")
_DEBT_KEY_NOISE_WORDS = frozenset(
    {"LTD", "LIMITED", "PVT", "PRIVATE", "THE", "AND", "OF", "IN", "LT"})
_DEBT_KEY_DATE_RE = re.compile(r"[()]?(\d{2})[./-](\d{2})[./-](\d{4})[()]?")
_DEBT_KEY_COUPON_PCT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,4})?)\s*%")
_DEBT_KEY_COUPON_BARE_RE = re.compile(r"\b(\d{1,2}\.\d{1,4})\b")


def _debt_instrument_key(company: str, coupon, maturity_date) -> str | None:
    """Issuer+coupon+maturity identity for a debt holding line.
    ``coupon`` / ``maturity_date`` come from the enriched row when available;
    otherwise they are recovered from the name (with or without '%' and
    parentheses). Returns None when neither can be found — i.e. the line isn't
    recognisably a dated instrument; those keep the legacy exact-name
    fallback."""
    text = (company or "").upper()
    mat = maturity_date or ""
    if not mat:
        m = _DEBT_KEY_DATE_RE.search(text)
        if m:
            mat = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    coup = coupon
    if coup is None:
        m = (_DEBT_KEY_COUPON_PCT_RE.search(text)
             or _DEBT_KEY_COUPON_BARE_RE.search(text))
        if m:
            try:
                coup = float(m.group(1))
            except ValueError:
                coup = None
    if coup is None and not mat:
        return None
    # Issuer tokens: drop the numeric artifacts + corporate noise words.
    text = _MATURITY_RE.sub(" ", text)
    text = _DEBT_KEY_DATE_RE.sub(" ", text)
    text = _DEBT_KEY_COUPON_PCT_RE.sub(" ", text)
    text = _DEBT_KEY_COUPON_BARE_RE.sub(" ", text)
    tokens = [t for t in _DEBT_KEY_NOISE_RE.sub(" ", text).split()
              if t not in _DEBT_KEY_NOISE_WORDS]
    parts = [" ".join(tokens)]
    if coup is not None:
        try:
            parts.append(f"C{round(float(coup), 4)}")
        except (TypeError, ValueError):
            pass
    if mat:
        parts.append(f"M{mat}")
    key = "|".join(p for p in parts if p)
    return key or None


def _bond_duration_metrics(coupon, maturity_date: str | None,
                           ytm=None, price=None) -> dict | None:
    """Modified/Macaulay duration (years) for a bond record [DBT5].

    ``maturity_date`` is 'YYYY-MM-DD'; tenor runs from today. Needs a
    positive tenor plus one yield source (YTM or clean price); returns None
    otherwise — callers just omit the field."""
    from .analytics import bullet_modified_duration

    if not maturity_date:
        return None
    try:
        yrs = (datetime.strptime(maturity_date, "%Y-%m-%d").date()
               - datetime.now().date()).days / 365.25
    except ValueError:
        return None
    if yrs <= 0:
        return None
    try:
        got = bullet_modified_duration(
            coupon_pct=float(coupon) if coupon is not None else None, years=yrs,
            ytm_pct=float(ytm) if ytm not in (None, 0) else None,
            price=float(price) if price not in (None, 0) else None)
    except (TypeError, ValueError):
        return None
    if got and got["modified_duration"] <= 0:
        return None
    return got




_DEBT_KEYWORDS = ("t-bill", "tbill", "treasury", "goi", "g-sec", "gsec", "gilt",
                  "sdl", "commercial paper", "certificate of deposit", "ncd",
                  "repo", "treps", "money market", "bank deposit", "debenture",
                  "zero coupon", " 91 day", " 182 day", " 364 day", " bonds")


def _cap_bucket(h: dict) -> str:
    """Classify a holding into a cap/asset bucket (canonical keys). Debt
    instruments and fund units are separated from equity cap segments so the
    'Unclassified' slice only contains genuinely untagged equity."""
    company = (h.get("company") or "").lower()
    sector = (h.get("sector") or "").lower()
    isin = (h.get("isin") or "").upper()
    asset = (h.get("asset_class") or "").lower()
    text = company + " " + sector
    if asset == "debt" or any(k in text for k in _DEBT_KEYWORDS) or re.search(r"\b(cp|cd)\b", text):
        return "debt"
    if re.search(r"\b(?:fund|etf)\b", company) or isin.startswith("INF"):
        return "fund units"
    cap = (h.get("cap") or "na").strip().lower()
    if cap in ("large", "mid", "small", "ipo"):
        return cap
    if cap in ("microcap", "sme"):
        return "microcap"
    return "unclassified"


_RATING_TOKEN_RE = re.compile(r"(AAA|AA\+?|A1\+?|A\+?|BBB\+?|BB\+?|B\+?)", re.IGNORECASE)


def _credit_bucket(rating, sector: str = "", section: str = "", company: str = "") -> str:
    """Credit bucket for a debt holding. Falls back to the sector field (which
    often carries the rating, e.g. 'CRISIL AAA') and to the instrument type for
    unrated government securities, so no debt is lumped as 'Unrated/Other'."""
    r = (rating or "").upper().strip()
    if not r and sector:
        m = _RATING_TOKEN_RE.search(sector.upper())
        if m:
            r = m.group(1).upper()
    if not r and section:
        m = _RATING_TOKEN_RE.search(section.upper())
        if m:
            r = m.group(1).upper()
    if not r or r in ("NA", "-", "UNRATED", "NR"):
        t = f"{section or ''} {sector or ''} {company or ''}".upper()
        if any(k in t for k in ("SOVEREIGN", "GOVT", "G-SEC", "TBILL", "TREASURY", "GILT",
                                "SDL", "SOV", "STATE GOVERNMENT", "TREPS")):
            r = "SOVEREIGN"
        else:
            return "Unrated/Other"
    for b in ("AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BB", "B"):
        if b in r:
            return b
    if any(k in r for k in ("SOVEREIGN", "GOVT", "G-SEC", "TBILL", "TREASURY", "GILT", "SDL", "STATE")) or r == "SOV":
        return "Sovereign/GSec"
    return r[:8]


def _instrument_type(company, sector, section: str = "") -> str:
    """Instrument type for a debt holding. The parsed ``section`` is the
    authoritative source (e.g. 'Certificates of Deposit', 'NCD & Bonds',
    'Treasury Bills'); company/sector keywords and the credit rating are the
    fallback for partial snapshots (issuer-only names with a rating)."""
    s = (section or "").upper()
    t = f"{company or ''} {sector or ''}".upper()
    if any(k in s for k in ("TREASURY", "T-BILL", "TBILL", "GOVERNMENT", "G-SEC", "GILT", "SDL")) \
            or any(k in t for k in ("TREASURY", "T-BILL", "TBILL", "GOVT", "G-SEC", "GOI", "GILT", "SDL")):
        return "Govt / SDL / T-Bill"
    if "CERTIFICATE OF DEPOSIT" in s or "CERTIFICATE OF DEPOSIT" in t or re.search(r"\bCD\b", s):
        return "Certificate of Deposit"
    if "COMMERCIAL PAPER" in s or "COMMERCIAL PAPER" in t or re.search(r"\bCP\b", s):
        return "Commercial Paper"
    if "NCD" in s or "NCD" in t or "NON CONVERTIBLE" in t:
        return "NCD"
    if "BOND" in s or "BOND" in t or "DEBENTURE" in s or "DEBENTURE" in t:
        return "Corporate Bond"
    if "REPO" in s or "TREPS" in s or "REPO" in t or "TREPS" in t:
        return "Repo / TREPS"
    if "MONEY MARKET" in s or "MONEY MARKET" in t:
        return "Money Market"
    if re.search(r"(?:^|\s)C\.?D\.?(?:\s|$)", t) or re.search(r"\sCP\s|\sCP$", t):
        return "Certificate of Deposit" if "CD" in t else "Commercial Paper"
    # Rating-driven fallback (issuer-only names that carry a rating):
    if any(k in t for k in ("SOVEREIGN", "G-SEC", "TBILL", "TREASURY", "GILT")) or re.search(r"\bSOV\b", t):
        return "Govt / SDL / T-Bill"
    if re.search(r"(A1\+|A\s?\d|A-1)", t):  # short-term (money-market) ratings
        return "Money Market"
    if _RATING_TOKEN_RE.search(t):  # long-term rating -> bond/NCD-type instrument
        return "Corporate Bond"
    return "Other"


def _debt_details(company: str, yield_str, sector: str) -> dict:
    """Extract coupon %, maturity date, YTM and rating from a debt holding."""
    coupon = None
    m = _COUPON_RE.search(company or "")
    if m:
        coupon = float(m.group(1))
    maturity = None
    m = _MATURITY_RE.search(company or "")
    if m:
        maturity = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    ytm = _num(yield_str)
    if ytm is not None and ytm < 1:
        ytm = round(ytm * 100, 4)  # fraction -> percent
    rating = None
    if sector and _RATING_LIKE_RE.search(sector):
        rating = sector.strip()
    return {"coupon": coupon, "maturity_date": maturity, "ytm": ytm, "rating": rating}


class _SafeCursor:
    """Cursor wrapper that holds the connection lock for each fetch/iterate."""

    def __init__(self, cursor, lock):
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, size=1):
        with self._lock:
            return self._cursor.fetchmany(size)

    def __iter__(self):
        with self._lock:
            rows = list(self._cursor)
        return iter(rows)

    def close(self):
        with self._lock:
            self._cursor.close()

    @property
    def lastrowid(self):
        with self._lock:
            return self._cursor.lastrowid

    @property
    def rowcount(self):
        with self._lock:
            return self._cursor.rowcount

    @property
    def description(self):
        with self._lock:
            return self._cursor.description


class _SafeConnection:
    """sqlite3 connection that can be shared across FastAPI worker threads.

    FastAPI runs synchronous endpoints in a threadpool, so a connection opened on
    one thread would fail with ``sqlite3.ProgrammingError`` when used on another.
    ``check_same_thread=False`` plus a lock that serialises every operation makes
    the connection safe to share (the DB is read-only after build).
    """

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, parameters=()):
        with self._lock:
            return _SafeCursor(self._conn.execute(sql, parameters), self._lock)

    def executemany(self, sql, seq_of_parameters):
        with self._lock:
            self._conn.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        with self._lock:
            self._conn.executescript(sql_script)

    def cursor(self):
        with self._lock:
            return _SafeCursor(self._conn.cursor(), self._lock)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def rollback(self):
        with self._lock:
            self._conn.rollback()

    def close(self):
        with self._lock:
            self._conn.close()


class WebDB:
    """Read-only query facade over the built SQLite database."""

    def __init__(self, path: Path = DB_PATH):
        self.con = _SafeConnection(path)
        self._canon: list | None = None
        self._eq_isins: set | None = None
        self._bonds_mtime: float = 0.0
        self._bonds: dict | None = None
        self._bonds_index: dict | None = None

    def _equity_isin_set(self) -> set:
        if self._eq_isins is None:
            self._eq_isins = {r[0] for r in self.con.execute(
                "SELECT isin FROM securities WHERE confirmed_equity=1")}
        return self._eq_isins

    # ---- meta / dashboard ----
    def meta_stats(self) -> dict:
        cur = self.con
        out = {
            "schemes": cur.execute("SELECT COUNT(*) FROM schemes").fetchone()[0],
            "schemes_with_holdings": cur.execute(
                "SELECT COUNT(*) FROM schemes WHERE coverage='has_holdings'").fetchone()[0],
            "holdings": cur.execute("SELECT COUNT(*) FROM holdings").fetchone()[0],
            "holdings_with_isin": cur.execute(
                "SELECT COUNT(*) FROM holdings WHERE isin!=''").fetchone()[0],
            "securities": cur.execute("SELECT COUNT(*) FROM securities").fetchone()[0],
            "pure_stocks": cur.execute(
                "SELECT COUNT(*) FROM securities WHERE confirmed_equity=1").fetchone()[0],
            "amcs": cur.execute("SELECT COUNT(DISTINCT amc) FROM schemes").fetchone()[0],
            "as_of": cur.execute(
                "SELECT MAX(as_of) FROM schemes WHERE as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
            ).fetchone()[0] or "",
        }
        out["isin_completeness"] = round(
            out["holdings_with_isin"] / out["holdings"] * 100, 1) if out["holdings"] else 0.0
        cap = dict(cur.execute(
            "SELECT cap, COUNT(*) FROM securities WHERE confirmed_equity=1 "
            "AND cap!='na' GROUP BY cap ORDER BY 2 DESC").fetchall())
        sector = dict(cur.execute(
            "SELECT sector, COUNT(*) FROM securities WHERE confirmed_equity=1 "
            "AND sector!='na' GROUP BY sector ORDER BY 2 DESC").fetchall())
        coverage = dict(cur.execute(
            "SELECT coverage, COUNT(*) FROM schemes GROUP BY coverage").fetchall())
        cat = dict(cur.execute(
            "SELECT category, COUNT(*) FROM schemes GROUP BY category ORDER BY 2 DESC").fetchall())
        out["cap_dist"] = cap
        out["sector_dist"] = sector
        out["coverage_dist"] = coverage
        out["category_dist"] = cat
        return out

    # ---- schemes ----
    def list_schemes(self, amc=None, category=None, source=None, coverage=None,
                     search=None, cap=None, sector=None, is_index=None, is_etf=None,
                     limit=100, offset=0) -> dict:
        where, args = [], []
        if amc:
            where.append("amc LIKE ?"); args.append(f"%{amc}%")
        if category:
            where.append("category=?"); args.append(category)
        if source:
            where.append("source=?"); args.append(source)
        if coverage:
            where.append("coverage=?"); args.append(coverage)
        if is_index is not None:
            where.append("is_index=?"); args.append(1 if is_index else 0)
        if is_etf is not None:
            where.append("is_etf=?"); args.append(1 if is_etf else 0)
        if search:
            where.append("(fund_name LIKE ? OR amc LIKE ? OR key LIKE ?)")
            args += [f"%{search}%", f"%{search}%", f"%{norm_name(search)}%"]
        if cap or sector:
            joins = ["schemes s"]
            if cap:
                joins.append("""JOIN holdings h ON h.scheme_id=s.id
                                JOIN securities sec ON sec.isin=h.isin AND sec.cap=?""")
                args.append(cap)
            if sector:
                joins.append("""JOIN holdings h2 ON h2.scheme_id=s.id
                                JOIN securities sec2 ON sec2.isin=h2.isin AND sec2.sector=?""")
                args.append(sector)
            sql_from = " FROM " + " ".join(joins)
            where_sql = " WHERE " + (" AND ".join(where) if where else "1=1")
            count_sql = "SELECT COUNT(DISTINCT s.id) " + sql_from + where_sql
            rows_sql = "SELECT DISTINCT s.* " + sql_from + where_sql
        else:
            sql_from = " FROM schemes s"
            where_sql = " WHERE " + (" AND ".join(where) if where else "1=1")
            count_sql = "SELECT COUNT(*) " + sql_from + where_sql
            rows_sql = "SELECT s.* " + sql_from + where_sql
        total = self.con.execute(count_sql, args).fetchone()[0]
        rows_sql += " ORDER BY s.aum DESC NULLS LAST, s.fund_name LIMIT ? OFFSET ?"
        rows = self.con.execute(rows_sql, args + [limit, offset]).fetchall()
        return {"total": total, "items": [self._scheme_dict(r) for r in rows]}

    def get_scheme(self, scheme_id: int) -> dict | None:
        r = self.con.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,)).fetchone()
        return self._scheme_dict(r) if r else None

    def find_schemes(self, names: list[str]) -> list[dict]:
        out = []
        for name in names:
            r = self.con.execute(
                "SELECT * FROM schemes WHERE fund_name=? COLLATE NOCASE OR key=? LIMIT 1",
                (name, norm_name(name))).fetchone()
            if r:
                out.append(self._scheme_dict(r))
        return out

    def scheme_holdings(self, scheme_id: int, limit: int | None = None) -> list[dict]:
        q = ("SELECT h.*, sec.cap AS cap, sec.confirmed_equity AS confirmed_equity "
             "FROM holdings h LEFT JOIN securities sec ON sec.isin = h.isin "
             "WHERE h.scheme_id=? ORDER BY h.percent_nav DESC NULLS LAST, h.company")
        args: list = [scheme_id]
        if limit:
            q += " LIMIT ?"; args.append(limit)
        rows = [dict(r) for r in self.con.execute(q, args).fetchall()]
        eq_isins = self._equity_isin_set()
        for r in rows:
            r["asset_class"] = refine_asset_class(
                r.get("asset_class") or "", r.get("confirmed_equity"), r.get("isin") or "")
            # Parse artifact guard: a pure-equity ISIN on a debt security (e.g.
            # "6.98 Gujarat SDL Nov 26 2032" matched to the Gujarat Mineral
            # equity ISIN) is wrong — clear it so the row stays a debt holding.
            if r.get("asset_class") != "stocks" and (r.get("isin") or "").upper() in eq_isins:
                r["isin"] = ""
                r["asset_class"] = refine_asset_class(
                    r.get("asset_class") or "", r.get("confirmed_equity"), "")
            if r["asset_class"] == "debt":
                r.update(_debt_details(r.get("company") or "", r.get("yield") or "",
                                       r.get("sector") or ""))
                self._enrich_bond_fields(r)
        return rows

    def scheme_nav(self, scheme_id: int, start: str | None = None,
                   end: str | None = None) -> dict:
        """Historical daily NAV for a scheme, split into Regular & Direct plans.

        Looks up each plan's AMFI scheme code in ``data/nav_history/<code>.json``
        (the downloaded AMFI history). Returns ``{regular, direct}`` where each is
        ``None`` when that plan's code has no history file. ``start``/``end`` are
        optional inclusive date filters (``YYYY-MM-DD`` or ``DD-Mon-YYYY``).
        """
        s = self.get_scheme(scheme_id)
        if not s:
            return {"regular": None, "direct": None}
        out = {}
        for plan, code in (("regular", s.get("amfi_regular")),
                           ("direct", s.get("amfi_direct"))):
            out[plan] = self._load_nav_plan(code, start=start, end=end)
        return out

    # ---- performance & risk analytics [ANA1] --------------------------------
    # Benchmark selection v2 [ANA1]: (1) the scheme's own tracked index
    # (schemes.index_name — strongest signal, bypasses the category gate);
    # (2) ordered word-boundary keyword rules over category+fund name —
    # MOST SPECIFIC FIRST (factor names with numbers before plain factors,
    # "nifty 500" before "nifty 50"); (3) equity-only default NIFTY 500.
    # Every index here has a TR series in data/nifty/TR/; a missing series
    # degrades to a '"<index> (series unavailable)"' label, never a wrong
    # number. Debt/hybrid without index_name stay unmapped (composite
    # benchmarks deferred — see docs/ANALYTICS_METHODOLOGY.md).
    _BENCHMARK_RULES: list[tuple[str, str]] = [
        (r"nifty\s*500\s*equal\s*weight", "NIFTY 500 EQUAL WEIGHT"),
        (r"equal\s*weight", "NIFTY 50 EQUAL WEIGHT"),
        (r"nifty\s*next\s*100", "NIFTY NEXT 100"),
        (r"nifty\s*next\s*50|junior", "NIFTY NEXT 50"),
        (r"200\s*momentum\s*30", "NIFTY200 MOMENTUM 30"),
        (r"100\s*alpha\s*30", "NIFTY100 ALPHA 30"),
        (r"200\s*quality\s*30", "NIFTY200 QUALITY 30"),
        (r"100\s*low\s*volatility\s*30", "NIFTY100 LOW VOLATILITY 30"),
        (r"200\s*value\s*30", "NIFTY200 VALUE 30"),
        (r"50\s*value\s*20", "NIFTY 50 VALUE 20"),
        (r"500\s*momentum\s*50", "NIFTY500 MOMENTUM 50"),
        (r"momentum", "NIFTY200 MOMENTUM 30"),
        (r"\balpha\b", "NIFTY100 ALPHA 30"),
        (r"quality", "NIFTY200 QUALITY 30"),
        (r"low\s*volatility", "NIFTY100 LOW VOLATILITY 30"),
        (r"\bvalue\b", "NIFTY200 VALUE 30"),
        (r"nifty\s*500", "NIFTY 500"),
        (r"nifty\s*200", "NIFTY 200"),
        (r"nifty\s*100", "NIFTY 100"),
        (r"nifty\s*50\b", "NIFTY 50"),
        (r"cnx\s*nifty\b|bees\b", "NIFTY 50"),
        (r"midcap\s*150|mid\s*cap\s*150", "NIFTY MIDCAP 150"),
        (r"smallcap\s*250|small\s*cap\s*250", "NIFTY SMALLCAP 250"),
        (r"midcap|mid\s*cap", "NIFTY MIDCAP 150"),
        (r"smallcap|small\s*cap", "NIFTY SMALLCAP 250"),
        (r"large\s*cap|largecap", "NIFTY 100"),
        (r"total\s*market", "NIFTY TOTAL MARKET"),
        (r"nifty\s*bank|\bbanks?\b|banking", "NIFTY BANK"),
        (r"\bit\b", "NIFTY IT"),
        (r"pharma", "NIFTY PHARMA"),
        (r"health\s*care|healthcare", "NIFTY HEALTHCARE"),
        (r"fmcg", "NIFTY FMCG"),
        (r"auto\b|automobiles?", "NIFTY AUTO"),
        (r"energy", "NIFTY ENERGY"),
        (r"\bmetals?\b", "NIFTY METAL"),
        (r"\bpower\b", "NIFTY POWER"),
        (r"consumption", "NIFTY INDIA CONSUMPTION"),
        (r"infrastructure", "NIFTY INFRASTRUCTURE"),
        (r"\bcpse\b", "NIFTY CPSE"),
        (r"\bpsu\b", "NIFTY PSE"),
        (r"\bmnc\b", "NIFTY MNC"),
        (r"dividend", "NIFTY DIVIDEND OPPORTUNITIES 50"),
    ]
    DEFAULT_EQUITY_BENCHMARK = "NIFTY 500"

    @classmethod
    def _benchmark_index_for(cls, s: dict) -> str | None:
        # 1) The fund's own tracked index (display form; _load_tr_index
        #    re-normalises to the CSV name).
        tracked = (s.get("index_name") or "").strip()
        if tracked:
            return tracked.replace("_", " ").upper()
        if (s.get("category") or "").lower() != "equity":
            return None
        text = ((s.get("category") or "") + " "
                + (s.get("fund_name") or "")).lower()
        for pattern, index in cls._BENCHMARK_RULES:
            if re.search(pattern, text):
                return index
        return cls.DEFAULT_EQUITY_BENCHMARK

    @staticmethod
    def _load_tr_index(index_name: str) -> list[tuple[str, float]] | None:
        """Total-return series [(ISO date, index level)] for a Nifty index."""
        fname = index_name.strip().upper().replace(" ", "_") + ".csv"
        path = NIFTY_TR_DIR / fname
        if not path.exists():
            # TR CSVs ship on R2 like nav_history — lazy-fetch on first use,
            # otherwise a fresh container reports every benchmark as
            # 'series unavailable' forever.
            remote_store.ensure(f"nifty/TR/{fname}")
        if not path.exists():
            return None
        try:
            rows = []
            with open(path, encoding="utf-8", errors="replace") as fh:
                next(fh)  # header: Date,TotalReturnsIndex,NTR_Value
                for ln in fh:
                    parts = ln.rstrip("\n").split(",")
                    if len(parts) < 2 or not parts[1].strip().replace(".", "", 1).isdigit():
                        continue
                    rows.append((parts[0].strip(), float(parts[1])))
            return rows or None
        except OSError:
            return None

    def scheme_analytics(self, scheme_id: int) -> dict:
        """Performance/risk suite for one scheme over its AMFI NAV history.

        Uses the Direct plan when available (investor-comparable), else
        Regular. Degrades honestly: missing history/benchmark -> explicit
        nulls, never fabricated numbers."""
        import os as _os
        from .analytics import DEFAULT_RF_PCT, compute_series_analytics

        s = self.get_scheme(scheme_id)
        if not s:
            return {}
        nav = self.scheme_nav(scheme_id)
        plan_used = "direct" if nav.get("direct") else \
            ("regular" if nav.get("regular") else None)
        doc = nav.get(plan_used) if plan_used else None
        base = {"scheme_id": scheme_id,
                "fund_name": s.get("fund_name"),
                "category": s.get("category"),
                "disclaimer":
                    "Past performance is not indicative of future returns."}
        if not doc:
            base["error"] = "no NAV history available for this scheme"
            return base
        try:
            rf = float(_os.environ.get("ANALYTICS_RF_PCT", "") or 0.0)
        except ValueError:
            rf = 0.0
        bench_name = self._benchmark_index_for(s)
        bench = self._load_tr_index(bench_name) if bench_name else None
        # Benchmark TR series file identity joins the cache key so a refreshed
        # CSV invalidates naturally; everything else keys off the scheme's own
        # last NAV date (data advance == cache miss).
        bench_mtime = 0.0
        if bench_name:
            _bp = NIFTY_TR_DIR / (bench_name.strip().upper()
                                  .replace(" ", "_") + ".csv")
            try:
                bench_mtime = _bp.stat().st_mtime if _bp.exists() else 0.0
            except OSError:
                bench_mtime = 0.0
        cache_key = (scheme_id, plan_used, doc.get("last_date"),
                     round(rf if rf > 0 else DEFAULT_RF_PCT, 2), bench_mtime)
        cached = _analytics_cache.get(cache_key)
        if cached is not None:
            return {**cached}
        out = compute_series_analytics(
            list(zip(doc["dates"], doc["navs"])),
            rf_pct=rf if rf > 0 else DEFAULT_RF_PCT,
            bench_series=bench)
        out.update(base)
        out["plan_used"] = plan_used
        out["benchmark_index"] = bench_name if bench else (
            None if not bench_name else f"{bench_name} (series unavailable)")
        # Per-scheme rolling-1Y series for the details chart [ANA2], and a
        # completeness badge (points / span / max gap) so the UI can state
        # exactly how much history backs every figure above.
        try:
            out["rolling_points"] = self._rolling_1y_points(doc["dates"],
                                                            doc["navs"])
        except Exception:
            out["rolling_points"] = []
        try:
            from src.nav_freshness import scheme_history_completeness
            out["history_completeness"] = \
                scheme_history_completeness(doc.get("code") or "")
        except Exception:
            out["history_completeness"] = None
        _analytics_cache[cache_key] = out
        if len(_analytics_cache) > _ANALYTICS_CACHE_MAX:
            _analytics_cache.pop(next(iter(_analytics_cache)))
        return out

    @staticmethod
    def _rolling_1y_points(dates: list[str], navs: list[float],                           step_days: int = 7) -> list[list[float]]:
        """Rolling 1Y return (%) at ~weekly steps for charting [ANA2]."""
        from datetime import timedelta

        from .analytics import parse_nav_date
        pts = []
        parsed = [(parse_nav_date(d), v) for d, v in zip(dates, navs)]
        parsed = [(d, v) for d, v in parsed if d and v and v > 0]
        j = 0
        next_take = 0
        win = timedelta(days=365)
        for i, (d, v) in enumerate(parsed):
            while j < len(parsed) and parsed[j][0] < d - win:
                j += 1
            if j >= len(parsed):
                break
            bd, bv = parsed[j]
            gap = (d - bd).days
            if i >= next_take and gap >= 355 and bv > 0:
                pts.append([d.isoformat(), round((v / bv - 1.0) * 100.0, 2)])
                next_take = i + max(1, step_days)
        return pts

    def _plan_series(self, scheme_id: int):
        """(plan_used, {iso_date: nav}) for one scheme — Direct preferred."""
        from .analytics import parse_nav_date
        nav = self.scheme_nav(scheme_id)
        plan = "direct" if nav.get("direct") else \
            ("regular" if nav.get("regular") else None)
        doc = nav.get(plan) if plan else None
        if not doc:
            return None, None
        m = {}
        for d, v in zip(doc["dates"], doc["navs"]):
            pd = parse_nav_date(d)
            if pd and v and v > 0:
                m[pd.isoformat()] = v
        return plan, m

    def portfolio_analytics(self, items: list[dict],
                            transactions: list[dict] | None = None) -> dict:
        """Performance/risk suite over a weighted scheme basket [ANA3].

        The client-portfolio NAV series is reconstructed as the
        weight-blended growth of its schemes on their common window
        (each scheme rebased to 100 at the window start), then run through
        the same metric engine as single schemes.

        When ``transactions`` (canonical, from
        ``tools_api.parse_cas_transactions``) accompany the items, the
        response additionally carries ``movement`` — the portfolio's ACTUAL
        cash-flow-aware value path (TWR daily chain + XIRR) [ANA3 movement].
        No transactions -> ``movement: None`` (honest, never fabricated)."""
        from .analytics import DEFAULT_RF_PCT, compute_series_analytics

        entries: list[tuple[int, float]] = []
        for it in items or []:
            if (it.get("type") or "").lower() not in ("scheme", "fund", "mf"):
                continue
            sid = it.get("id")
            try:
                sid = int(sid) if sid else None
            except (TypeError, ValueError):
                sid = None
            if not sid:
                s = self._resolve_scheme_item(it)
                sid = s["id"] if s else None
            if not sid:
                continue
            w = float(it.get("weight") or 0.0)
            if w > 0 and all(sid != e[0] for e in entries):
                entries.append((sid, w))
        if len(entries) < 1:
            return {"error": "no resolvable scheme lines with weights",
                    "disclaimer":
                        "Past performance is not indicative of future returns."}
        total_w = sum(w for _, w in entries)

        series_map: dict[int, dict[str, float]] = {}
        plans: dict[int, str] = {}
        for sid, _w in entries:
            plan, m = self._plan_series(sid)
            if m:
                series_map[sid] = m
                plans[sid] = plan or "?"
        if len(series_map) < 1:
            return {"error": "none of the portfolio schemes have NAV history",
                    "disclaimer":
                        "Past performance is not indicative of future returns."}

        used = [(sid, w / total_w * 100.0) for sid, w in entries if sid in series_map]
        starts = [min(m) for m in series_map.values()]
        ends = [max(m) for m in series_map.values()]
        start_iso, end_iso = max(starts), min(ends)
        enough = (date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days >= 90

        # master grid = densest scheme between start..end
        grids = {}
        for sid, m in series_map.items():
            grids[sid] = [k for k in sorted(m) if start_iso <= k <= end_iso]
        master_sid = max(grids, key=lambda k: len(grids[k]))
        grid = grids[master_sid]

        dates_out, values_out = [], []
        last = {sid: None for sid in series_map}
        for k in grid:
            port = 0.0
            ok = True
            for sid, w in used:
                v = series_map[sid].get(k) or last[sid]
                if v is None:
                    ok = False  # this line hadn't launched yet at window start
                    break
                last[sid] = v
                port += w * v / series_map[sid][start_iso]
            if not ok:
                continue
            dates_out.append(k)
            values_out.append(round(port, 4))

        import os as _os
        try:
            rf = float(_os.environ.get("ANALYTICS_RF_PCT", "") or 0.0)
        except ValueError:
            rf = 0.0
        out = compute_series_analytics(
            list(zip(dates_out, values_out)),
            rf_pct=rf if rf > 0 else DEFAULT_RF_PCT)
        out.update({
            "kind": "portfolio",
            "constituents": [{"scheme_id": sid,
                              "fund_name": self.get_scheme(sid)["fund_name"],
                              "weight": round(w, 2),
                              "plan_used": plans.get(sid, "?")}
                             for sid, w in sorted(used, key=lambda e: -e[1])],
            "window": {"start": dates_out[0] if dates_out else None,
                       "end": dates_out[-1] if dates_out else None},
            "window_common": enough,
            "growth_100": {"dates": dates_out, "values": values_out} if values_out else None,
            "disclaimer": "Past performance is not indicative of future returns.",
        })
        # [ANA3 movement] cash-flow-aware value path from real transactions.
        if transactions:
            from .analytics import portfolio_movement_series
            mv = portfolio_movement_series(items, transactions,
                                           self._movement_nav_map)
            out["movement"] = mv
            out["movement_source"] = "cas_transactions"
        else:
            out["movement"] = None
            out["movement_source"] = None
        return out

    def _movement_nav_map(self, amfi_code: str, isin: str) -> dict[str, float] | None:
        """{iso_date: nav} for one scheme referenced by a transaction record.

        Uses the record's AMFI code when present (fast path), else the
        statement ISIN -> schemes table -> plan codes. Direct plan preferred,
        Regular fallback — same investor-comparable convention as elsewhere.
        """
        from .analytics import parse_nav_date

        def _map(doc) -> dict[str, float] | None:
            if not doc or not doc["dates"]:
                return None
            out = {}
            for d, v in zip(doc["dates"], doc["navs"]):
                pd = parse_nav_date(d)
                if pd:
                    out[pd.isoformat()] = v
            return out or None

        for code in (amfi_code,):
            if code:
                doc = self._load_nav_plan(code)
                got = _map(doc)
                if got:
                    return got
        if isin:
            row = self.con.execute(
                "SELECT amfi_regular, amfi_direct FROM schemes "
                "WHERE isin_regular=? OR isin_direct=?",
                (isin.upper(), isin.upper())).fetchone()
            if row:
                for code in (row["amfi_direct"], row["amfi_regular"]):
                    if not code:
                        continue
                    doc = self._load_nav_plan(code)
                    got = _map(doc)
                    if got:
                        return got
        return None

    def compare_schemes(self, scheme_ids: list[int]) -> dict:
        """Side-by-side analytics for 2-12 schemes [ANA2].

        Growth-of-R100 is computed over the COMMON calendar window (latest
        shared start -> earliest shared last NAV), forward-filled onto the
        densest scheme's business-day grid so the lines are directly
        comparable. Rolling-1Y uses each scheme's own full history."""
        from .analytics import DEFAULT_RF_PCT, compute_series_analytics, parse_nav_date

        ids = [int(i) for i in (scheme_ids or [])][:12]
        picked = []
        for sid in ids:
            s = self.get_scheme(sid)
            if not s:
                continue
            nav = self.scheme_nav(sid)
            plan_used = "direct" if nav.get("direct") else \
                ("regular" if nav.get("regular") else None)
            doc = nav.get(plan_used) if plan_used else None
            if not doc or not doc["dates"]:
                continue
            picked.append((s, plan_used, doc))
        if len(picked) < 2:
            return {"schemes": [],
                    "error": "need at least two schemes with NAV history",
                    "disclaimer":
                        "Past performance is not indicative of future returns."}

        # Common window on ISO keys.
        def iso_bounds(doc):
            ds = [parse_nav_date(d) for d in doc["dates"]]
            return ds[0], ds[-1]

        start = max(iso_bounds(doc)[0] for _, _, doc in picked)
        end = min(iso_bounds(doc)[1] for _, _, doc in picked)
        enough = (end - start).days >= 90

        # Master grid: the scheme with most points inside the window.
        grids = []
        for s, plan, doc in picked:
            g = [(d, v) for d, v in
                 ((parse_nav_date(dd), vv) for dd, vv in zip(doc["dates"], doc["navs"]))
                 if d and start <= d <= end and v and v > 0]
            grids.append(g)
        master_idx = max(range(len(grids)), key=lambda k: len(grids[k]))
        master = grids[master_idx]

        out_schemes = []
        for k, (s, plan, doc) in enumerate(picked):
            try:
                rf = float(os.environ.get("ANALYTICS_RF_PCT", "") or 0.0)
            except ValueError:
                rf = 0.0
            bench_name = self._benchmark_index_for(s)
            bench = self._load_tr_index(bench_name) if bench_name else None
            a = compute_series_analytics(
                list(zip(doc["dates"], doc["navs"])),
                rf_pct=rf if rf > 0 else DEFAULT_RF_PCT, bench_series=bench)
            a.pop("disclaimer", None)

            growth = None
            if enough and master:
                # Forward-fill this scheme onto the master grid, rebase to 100.
                own = dict((d.isoformat(), v) for d, v in grids[k])
                last_v = None
                gv, gd = [], []
                for d, _v in master:
                    v = own.get(d.isoformat())
                    if v is None and last_v is not None:
                        v = last_v
                    elif v is None:
                        continue  # scheme not yet launched inside the window
                    last_v = v
                    gd.append(d.isoformat())
                    gv.append(v)
                if len(gd) >= 30 and gv[0]:
                    base0 = gv[0]
                    growth = {"dates": gd,
                              "values": [round(100.0 * v / base0, 4) for v in gv]}

            rolling = self._rolling_1y_points(doc["dates"], doc["navs"])
            out_schemes.append({
                "id": s["id"], "fund_name": s.get("fund_name"),
                "amc": s.get("amc"), "category": s.get("category"),
                "plan_used": plan,
                "metrics": a,
                "growth_100": growth,
                "rolling_1y": {"dates": [p[0] for p in rolling],
                               "values": [p[1] for p in rolling]} if rolling else None,
                "benchmark_index": bench_name if bench else None,
            })

        return {
            "as_of": end.isoformat(),
            "window": {"start": start.isoformat(), "end": end.isoformat(),
                       "common": enough},
            "rf_pct_assumption": DEFAULT_RF_PCT,
            "schemes": out_schemes,
            "disclaimer": "Past performance is not indicative of future returns.",
        }

    @staticmethod
    def _load_nav_plan(code, start: str | None = None, end: str | None = None) -> dict | None:
        if not code:
            return None
        path = NAV_HISTORY_DIR / f"{code}.json"
        if not path.exists():
            remote_store.ensure(f"nav_history/{code}.json")
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            return None
        history = doc.get("history") or []
        # [NAV-STUB] A suspiciously thin file may be shadowing the full R2
        # history (cold-start stubs). One upgrade attempt per code/process.
        if len(history) < NAV_STUB_HEAL_MIN_POINTS:
            better = _heal_thin_nav_history(code, len(history))
            if better is not None:
                doc, history = better, better.get("history") or []
        if not history:
            return None
        # History files are stored in lexicographic 'DD-Mon-YYYY' order (all
        # day-01 rows first) — sort chronologically so the line chart draws a
        # proper time series instead of a zigzag.
        history = sorted(history, key=lambda h: _nav_date_key(h.get("date", "")) or "")
        # Optional inclusive date-range filter.
        if start or end:
            start_key = _nav_date_key(start) if start else ""
            end_key = _nav_date_key(end) if end else ""
            history = [h for h in history
                       if (not start_key or _nav_date_key(h["date"]) >= start_key)
                       and (not end_key or _nav_date_key(h["date"]) <= end_key)]
            if not history:
                return None
        dates = [h["date"] for h in history]
        navs = [h["nav"] for h in history]
        n = len(history)
        return {
            "code": code,
            "fund_name": doc.get("fund_name") or "",
            "currency": doc.get("currency", "INR"),
            "source": doc.get("source", "AMFI"),
            "points": n,
            "inception": history[0]["date"],
            "first_nav": history[0]["nav"],
            "last_nav": history[-1]["nav"],
            "last_date": history[-1]["date"],
            "dates": dates,
            "navs": navs,
        }

    def _scheme_dict(self, r) -> dict:
        d = dict(r)
        d["as_of"] = _clean_holdings_date(d.get("as_of"))
        return d

    def holdings_stats(self, scheme_ids) -> dict[int, dict]:
        """scheme_id -> {n, with_isin, with_pct, max_pct, sum_pct} over the
        holdings table (batch helper for confidence scoring)."""
        ids = [int(i) for i in (scheme_ids or [])]
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        rows = self.con.execute(
            f"SELECT scheme_id, COUNT(*) AS n, "
            f"SUM(CASE WHEN isin!='' THEN 1 ELSE 0 END) AS wi, "
            f"SUM(CASE WHEN percent_nav IS NOT NULL THEN 1 ELSE 0 END) AS wp, "
            f"MAX(percent_nav) AS max_pct, SUM(percent_nav) AS sum_pct "
            f"FROM holdings WHERE scheme_id IN ({q}) GROUP BY scheme_id",
            ids).fetchall()
        return {r["scheme_id"]: {"n": r["n"], "with_isin": r["wi"] or 0,
                                 "with_pct": r["wp"] or 0,
                                 "max_pct": r["max_pct"],
                                 "sum_pct": r["sum_pct"]} for r in rows}

    # ---- stocks (price / actions / reports) ----
    def stock_price(self, isin: str, start: str | None = None,
                    end: str | None = None) -> dict | None:
        """Daily close history for a stock (live file read, mirrors NAV history)."""
        path = STOCK_HISTORY_DIR / f"{isin}.json"
        if not path.exists():
            remote_store.ensure(f"stock_history/{isin}.json")
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            return None
        history = doc.get("history") or []
        if not history:
            return None
        if start or end:
            start_key = _nav_date_key(start) if start else ""
            end_key = _nav_date_key(end) if end else ""
            history = [h for h in history
                       if (not start_key or _nav_date_key(h.get("date", "")) >= start_key)
                       and (not end_key or _nav_date_key(h.get("date", "")) <= end_key)]
            if not history:
                return None
        dates = [h["date"] for h in history]
        closes = [h["close"] for h in history]
        return {
            "isin": isin,
            "symbol": doc.get("symbol", ""),
            "name": doc.get("name", ""),
            "currency": doc.get("currency", "INR"),
            "source": doc.get("source", ""),
            "points": len(history),
            "inception": history[0]["date"],
            "first_close": history[0]["close"],
            "last_close": history[-1]["close"],
            "last_date": history[-1]["date"],
            "dates": dates,
            "closes": closes,
        }

    @staticmethod
    def _load_stock_actions(isin: str) -> dict | None:
        path = STOCK_ACTIONS_DIR / f"{isin}.json"
        if not path.exists():
            remote_store.ensure(f"stock_actions/{isin}.json")
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    @staticmethod
    def _load_stock_reports(isin: str) -> dict | None:
        path = STOCK_REPORTS_DIR / f"{isin}.json"
        if not path.exists():
            remote_store.ensure(f"stock_reports/{isin}.json")
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def stock_actions(self, isin: str) -> dict | None:
        return self._load_stock_actions(isin)

    def stock_reports(self, isin: str) -> dict | None:
        return self._load_stock_reports(isin)

    def list_securities(self, q=None, confirmed_equity=None, cap=None, sector=None,
                        limit=100, offset=0) -> dict:
        where, args = [], []
        if q:
            where.append("(name LIKE ? OR isin LIKE ? OR aliases LIKE ?)")
            args += [f"%{q}%", f"%{q.upper()}%", f"%{q}%"]
        if confirmed_equity is not None:
            where.append("confirmed_equity=?"); args.append(confirmed_equity)
        if cap:
            where.append("cap=?"); args.append(cap)
        if sector:
            where.append("sector=?"); args.append(sector)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.con.execute(f"SELECT COUNT(*) FROM securities{w}", args).fetchone()[0]
        rows = self.con.execute(
            f"SELECT * FROM securities{w} ORDER BY source_count DESC, name LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def get_security(self, isin: str) -> dict | None:
        r = self.con.execute("SELECT * FROM securities WHERE isin=?", (isin,)).fetchone()
        return dict(r) if r else None

    def security_usage(self, isin: str, limit=20) -> list[dict]:
        rows = self.con.execute(
            """SELECT fund_name, amc, percent_nav, as_of, source FROM holdings
               WHERE isin=? AND percent_nav IS NOT NULL
               ORDER BY percent_nav DESC LIMIT ?""", (isin, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---- bonds (NSE debt market) ----
    def _bond_catalog(self) -> dict:
        """Lazy, mtime-cached load of data/reference/bonds_catalog.json."""
        if not BONDS_CATALOG_JSON.exists():
            remote_store.ensure("reference/bonds_catalog.json")
        try:
            mtime = BONDS_CATALOG_JSON.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._bonds is None or mtime != self._bonds_mtime:
            self._bonds_mtime = mtime
            try:
                self._bonds = json.loads(BONDS_CATALOG_JSON.read_text(encoding="utf-8"))
            except Exception:
                self._bonds = {}
        return self._bonds

    def _bond_catalog_index(self) -> dict:
        if self._bonds_index is None:
            cat = self._bond_catalog()
            idx = {}
            for b in (cat.get("bonds") or []):
                isin = (b.get("isin") or "").strip().upper()
                if isin:
                    idx[isin] = b
            self._bonds_index = idx
        return self._bonds_index

    def _enrich_bond_fields(self, r: dict) -> None:
        """Backfill coupon / maturity / rating / YTM on a debt-holding row from
        the bond-market catalog (NSE bulk data + computed YTM). Never overrides
        values already extracted from the fund's own disclosure text."""
        isin = (r.get("isin") or "").strip().upper()
        if not isin:
            return
        rec = self._bond_catalog_index().get(isin)
        if not rec:
            return
        if (not r.get("coupon")) and rec.get("coupon"):
            r["coupon"] = rec["coupon"]
        if (not r.get("maturity_date")) and rec.get("maturity_date"):
            r["maturity_date"] = rec["maturity_date"]
        if (not r.get("rating")) and rec.get("rating"):
            r["rating"] = rec["rating"]
        # YTM: the fund text wins; the catalog supplies NSE-reported or
        # computed YTM (coupon + price + maturity) when the fund gives none.
        if (r.get("ytm") in (None, 0)) and rec.get("ytm") is not None and rec["ytm"] > 0:
            r["ytm"] = round(float(rec["ytm"]), 4)
            r["ytm_source"] = rec.get("ytm_source") or ""
        # Modified duration [DBT5] — computed, never stored.
        if r.get("modified_duration") is None:
            m = _bond_duration_metrics(r.get("coupon"), r.get("maturity_date"),
                                       ytm=r.get("ytm"))
            if m:
                r["modified_duration"] = m["modified_duration"]
                r["macaulay_duration"] = m["macaulay_duration"]


    def list_bonds(self, q=None, segment=None, rating=None, status=None,
                   maturity=None, only_traded=None, sort="ytm_desc",
                   limit=50, offset=0) -> dict:
        """NSE bond-market listing (ISIN, issuer, coupon, price, YTM, …).
        ``maturity`` buckets: '<1y' | '1-3y' | '3-7y' | '7-10y' | '10y+'."""
        cat = self._bond_catalog()
        bonds = list(cat.get("bonds") or [])
        total = len(bonds)
        if not (q or segment or rating or status or maturity or only_traded):
            slice_ = bonds[offset:offset + limit] if limit else bonds[offset:]
            items = [dict(b) for b in slice_]
            for b in items:
                b["rating_band"] = _credit_bucket(b.get("rating"), "", "",
                                                  b.get("name") or "")
            return {"total": total, "as_of": cat.get("as_of"),
                    "sources": cat.get("sources"), "items": items}
        today = datetime.now().date()
        nq = (q or "").strip().lower()
        seg = (segment or "").strip()
        rat = (rating or "").strip()
        st = (status or "").strip()
        mat = (maturity or "").strip()
        out = []
        for b in bonds:
            isin = (b.get("isin") or "").upper()
            name = (b.get("name") or "") + " " + (b.get("issuer") or "")
            if nq and nq not in isin.lower() and nq not in name.lower():
                continue
            if seg and (b.get("segment") or "") != seg:
                continue
            if st and (b.get("status") or "").lower() != st.lower():
                continue
            if rat:
                band = _credit_bucket(b.get("rating"), "", "", b.get("name") or "")
                if band != rat:
                    continue
            if mat:
                years = (datetime.strptime(b["maturity_date"], "%Y-%m-%d").date() - today).days / 365.0 \
                    if b.get("maturity_date") else None
                if years is None:
                    continue
                bucket = ("<1y" if years < 1 else "1-3y" if years < 3 else
                          "3-7y" if years < 7 else "7-10y" if years < 10 else "10y+")
                if bucket != mat:
                    continue
            if only_traded and not float_state(b.get("price")):
                continue
            b = dict(b)
            b["rating_band"] = _credit_bucket(b.get("rating"), "", "",
                                              b.get("name") or "")
            out.append(b)
        total = len(out)
        key_map = {
            "ytm_desc": ("ytm", True), "ytm_asc": ("ytm", False),
            "price_desc": ("price", True), "price_asc": ("price", False),
            "coupon_desc": ("coupon", True),
            "maturity_asc": ("maturity_date", False), "maturity_desc": ("maturity_date", True),
            "name_asc": ("name", False),
        }
        key, desc = key_map.get(sort, ("ytm", True))

        def sk(b):
            v = b.get(key)
            if v is None:
                return (1, 0)
            return (0, -v if desc and isinstance(v, (int, float)) else v)
        out.sort(key=sk)
        return {"total": total, "as_of": cat.get("as_of"),
                "sources": cat.get("sources"),
                "items": out[offset:offset + limit] if limit else out}

    def get_bond(self, isin: str) -> dict | None:
        b = self._bond_catalog_index().get((isin or "").strip().upper())
        if not b:
            return None
        b = dict(b)
        b["rating_band"] = _credit_bucket(b.get("rating"), "", "", b.get("name") or "")
        m = _bond_duration_metrics(b.get("coupon"), b.get("maturity_date"),
                                   ytm=b.get("ytm"), price=b.get("price"))  # [DBT5]
        if m:
            b["modified_duration"] = m["modified_duration"]
            b["macaulay_duration"] = m["macaulay_duration"]
        return b

    def bond_facets(self) -> dict:
        """Filter facets + summary for the Bonds tab."""
        cat = self._bond_catalog()
        segs: dict[str, int] = {}
        ratings: dict[str, int] = {}
        statuses: dict[str, int] = {}
        n_traded = 0
        n_ytm = 0
        for b in cat.get("bonds") or []:
            segs[(b.get("segment") or "Other")] = \
                segs.get((b.get("segment") or "Other"), 0) + 1
            band = _credit_bucket(b.get("rating"), "", "", b.get("name") or "")
            ratings[band] = ratings.get(band, 0) + 1
            st = (b.get("status") or "Listed").strip() or "Listed"
            statuses[st] = statuses.get(st, 0) + 1
            if float_state(b.get("price")):
                n_traded += 1
            if b.get("ytm") is not None:
                n_ytm += 1
        return {
            "as_of": cat.get("as_of"),
            "fetched_at": cat.get("fetched_at"),
            "sources": cat.get("sources"),
            "segments": {"items": sorted(segs, key=lambda k: -segs[k]),
                         "counts": segs},
            "ratings": {"items": sorted(ratings, key=lambda k: -ratings[k]),
                        "counts": ratings},
            "statuses": {"items": sorted(statuses, key=lambda k: -statuses[k]),
                         "counts": statuses},
            "n_bonds": len(cat.get("bonds") or []),
            "n_traded": n_traded,
            "n_with_ytm": n_ytm,
            "coverage": self.bond_coverage(),
        }

    def bond_coverage(self) -> dict:
        """Diagnostic split: how many fund YTM/coupon gaps are extraction
        ERRORS (data exists in NSE records but was not captured) versus NO INFO
        (the fund/bond genuinely does not disclose the number).

        Scheme level — every debt-fund scheme's ``ytm`` field:
          'with_ytm'        fund linked to the universe row that has YTM.
          'missing'         split into 'unmatched_name' (fund exists in the
                            universe but is stored under a wrapper scheme row
                            -> merge gap, fixable) and 'not_in_universe'.
        Holding level — the %NAV-level debt securities of all schemes:
          YTM present: 'fund text' / 'NSE reported' / 'computed (NSE price)'.
          YTM missing: 'no ISIN' / 'not in NSE catalog' / 'never traded' /
                       'perpetual' / 'other'.
          Coupon present: 'fund text' / 'NSE catalog'.
          Coupon N/A: zero-coupon / floating-rate instruments.
          Coupon missing: 'no ISIN' / 'not in NSE catalog' /
                          'no fixed coupon in record' / 'extraction gap'
                          (NSE name carries '%' but parser missed it — a true
                          data-handling error).
        """
        idx = self._bond_catalog_index()
        hs = [dict(r) for r in self.con.execute(
            "SELECT isin, company, yield, asset_class FROM holdings "
            "WHERE asset_class='debt'").fetchall()]

        scheme_rows = [dict(r) for r in self.con.execute(
            "SELECT fund_name, ytm FROM schemes WHERE category='Debt'").fetchall()]
        with_ytm = sum(1 for r in scheme_rows if r["ytm"] is not None)
        missing = [r for r in scheme_rows if r["ytm"] is None]

        ytm_present = {"fund_text": 0, "nse_reported": 0, "computed": 0}
        ytm_missing = {"no_isin": 0, "not_in_catalog": 0, "never_traded": 0,
                       "perpetual": 0, "other": 0}
        coupon_present = {"fund_text": 0, "catalog": 0}
        coupon_na = 0
        coupon_missing = {"no_isin": 0, "not_in_catalog": 0,
                          "no_fixed_coupon": 0, "extraction_gap": 0}
        _pct = self._pct_or_none
        for h in hs:
            isin = (h.get("isin") or "").strip().upper()
            comp = h.get("company") or ""
            y = _pct(h.get("yield"))
            if y is not None:
                ytm_present["fund_text"] += 1
            elif isin and isin in idx:
                rec = idx[isin]
                if rec.get("ytm"):
                    src = rec.get("ytm_source") or ""
                    if "computed" in src or "current yield" in src:
                        ytm_present["computed"] += 1
                    else:
                        ytm_present["nse_reported"] += 1
                elif rec.get("last_yield") or rec.get("wa_yield"):
                    ytm_present["nse_reported"] += 1
                elif not (rec.get("price") or rec.get("last_price") or
                          rec.get("last_yield") or rec.get("wa_yield")):
                    ytm_missing["never_traded"] += 1
                elif not rec.get("maturity_date"):
                    ytm_missing["perpetual"] += 1
                else:
                    ytm_missing["other"] += 1
            elif not isin:
                ytm_missing["no_isin"] += 1
            else:
                ytm_missing["not_in_catalog"] += 1

            if _COUPON_PCT_RE.search(comp):
                coupon_present["fund_text"] += 1
            elif isin and isin in idx:
                rec = idx[isin]
                if float_state(rec.get("coupon")):
                    coupon_present["catalog"] += 1
                elif _zero_or_floater(rec) or _zero_or_floater_name(comp):
                    coupon_na += 1
                elif _COUPON_PCT_RE.search(str(rec.get("name") or "")):
                    coupon_missing["extraction_gap"] += 1
                elif not rec.get("name"):
                    coupon_missing["no_fixed_coupon"] += 1
                else:
                    coupon_missing["no_fixed_coupon"] += 1
            elif not isin:
                coupon_missing["no_isin"] += 1
            else:
                coupon_missing["not_in_catalog"] += 1

        n = len(hs)
        out = {
            "schemes": {
                "debt_funds": len(scheme_rows),
                "with_ytm": with_ytm,
                "missing_ytm": len(missing),
                "missing_breakdown": self._scheme_ytm_gaps(missing),
            },
            "holdings": {
                "total": n,
                "ytm_present": ytm_present,
                "ytm_present_pct": round(sum(ytm_present.values()) / n * 100, 1) if n else 0,
                "ytm_missing": ytm_missing,
                "coupon_present": coupon_present,
                "coupon_present_pct": round(sum(coupon_present.values()) / n * 100, 1) if n else 0,
                "coupon_not_applicable": coupon_na,
                "coupon_not_applicable_pct": round(coupon_na / n * 100, 1) if n else 0,
                "coupon_missing": coupon_missing,
            },
        }
        errors = (coupon_missing["extraction_gap"]
                  + out["schemes"]["missing_breakdown"].get("unmatched_name", 0))
        no_info = (sum(ytm_missing.values()) + sum(coupon_missing.values())
                   - coupon_missing["extraction_gap"])
        out["summary"] = {
            "error_rows": errors,
            "no_info_rows": no_info + coupon_na,
            "coupon_na_rows": coupon_na,
        }
        return out

    def _scheme_ytm_gaps(self, missing: list) -> dict:
        """Classify debt schemes missing a YTM: name-merge gap vs genuinely
        absent from the universe feed."""
        unmatched = []
        not_in_uni = []
        for r in missing:
            name = r.get("fund_name") or ""
            # 'Portfolio of X as on ...' wrapper -> the underlying fund exists in
            # the universe under its clean name: a matching gap, not absence.
            clean = re.sub(r"^Portfolio\s+of\s+", "", name, flags=re.I)
            clean = _TRAILING_DATE_RE.sub("", clean).rstrip(" -–—:·")
            if clean and clean != name:
                unmatched.append(name)
            else:
                not_in_uni.append(name)
        return {"unmatched_name": len(unmatched), "not_in_universe": len(not_in_uni),
                "examples": unmatched[:6] + not_in_uni[:4]}

    def _holding_ytm_pct(self, h: dict) -> float | None:
        """YTM (%) for a debt holding: the fund's own yield text first, then the
        bond catalog's reported OR computed YTM (NSE price + coupon + maturity)."""
        y = self._pct_or_none(h.get("yield"))
        if y is not None:
            return round(y, 4)
        y = self._pct_or_none(h.get("ytm"))
        if y is not None:
            return round(y, 4)
        return None

    # ---- overlap ----
    def overlap(self, scheme_ids: list[int]) -> dict:
        schemes = [self.get_scheme(i) for i in scheme_ids]
        schemes = [s for s in schemes if s]
        lookup = {s["id"]: s for s in schemes}
        per_scheme: list[tuple[int, list[dict]]] = []
        for sid in scheme_ids:
            if sid not in lookup:
                continue
            per_scheme.append((sid, self.scheme_holdings(sid)))

        # [DBT4] ISIN alias map: for every dated debt line that DOES carry an
        # ISIN, remember its issuer+coupon+maturity identity so the same bond
        # reported without an ISIN elsewhere still overlaps with it.
        isin_alias: dict[str, str] = {}
        for _, holdings in per_scheme:
            for h in holdings:
                isin = (h.get("isin") or "").strip()
                if not isin:
                    continue
                dk = _debt_instrument_key(h.get("company") or "",
                                          h.get("coupon"), h.get("maturity_date"))
                if dk:
                    isin_alias.setdefault(dk, isin)

        weights: dict[int, dict[str, float]] = {}
        company_meta: dict[str, dict] = {}
        for sid, holdings in per_scheme:
            wmap: dict[str, float] = {}
            has_pct = False
            for h in holdings:
                isin = (h.get("isin") or "").strip()
                dk = _debt_instrument_key(h.get("company") or "",
                                          h.get("coupon"), h.get("maturity_date"))
                # key priority: explicit ISIN -> instrument identity resolved to
                # its ISIN via the alias pass -> raw identity -> exact name
                key = (isin or (dk and isin_alias.get(dk)) or dk
                       or (h.get("company") or "").strip())
                if not key:
                    continue
                p = h.get("percent_nav")
                wmap[key] = wmap.get(key, 0.0) + (p or 0.0)
                company_meta.setdefault(key, {"company": h.get("company", ""),
                                              "isin": h.get("isin", ""),
                                              "sector": h.get("sector", "")})
                if p is not None:
                    has_pct = True
            if not has_pct and wmap:
                # index/benchmark holdings without weights -> assume equal weight
                w = 100.0 / len(wmap)
                for k in wmap:
                    wmap[k] = w
            weights[sid] = wmap

        ids = list(weights.keys())
        matrix: list[dict] = []
        for i, a in enumerate(ids):
            row = {"scheme": lookup[a]["fund_name"], "id": a}
            for b in ids:
                inter = set(weights[a]) & set(weights[b])
                ov = round(sum(min(weights[a][k], weights[b][k]) for k in inter), 2)
                row[f"c_{b}"] = ov
            row["self"] = round(sum(weights[a].values()), 2)
            matrix.append(row)

        # concentration: total weight per common underlying holding
        conc: dict[str, float] = {}
        for sid in ids:
            for k, w in weights[sid].items():
                conc[k] = conc.get(k, 0.0) + w
        concentration = sorted(
            ([{**company_meta[k], "total_pct": round(v, 2)} for k, v in conc.items()]),
            key=lambda x: -x["total_pct"])[:10]

        # debt risk for debt-category schemes
        debt_risk = []
        for s in schemes:
            if (s.get("category") or "").lower() != "debt":
                continue
            debt_risk.append({
                "scheme": s["fund_name"],
                "ytm": s.get("ytm"), "duration": s.get("duration"),
                "avg_maturity": s.get("avg_maturity"),
                "top": self.scheme_holdings(s["id"], limit=5),
            })

        return {
            "schemes": [lookup[i] for i in ids],
            "ids": ids,
            "matrix": matrix,
            "concentration": concentration,
            "debt_risk": debt_risk,
            "disclaimer": True,
        }

    # ---- portfolio tools ----
    def _resolve_scheme_item(self, item: dict) -> dict | None:
        sid = item.get("id")
        if sid:
            try:
                s = self.get_scheme(int(sid))
                if s:
                    return s
            except (TypeError, ValueError):
                pass
        isin = (item.get("isin") or "").strip().upper()
        if isin:
            r = self.con.execute(
                "SELECT * FROM schemes WHERE isin_regular=? OR isin_direct=? LIMIT 1",
                (isin, isin)).fetchone()
            if r:
                return self._scheme_dict(r)
        name = (item.get("name") or "").strip()
        if not name:
            return None
        r = self.con.execute(
            "SELECT * FROM schemes WHERE fund_name=? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
        if r:
            return self._scheme_dict(r)
        # Strip plan/option suffixes (e.g. '- Direct Plan - Growth') so a CAS
        # scheme name matches the fund-level scheme stored in the DB.
        try:
            clean = fund_name_from_nav(name).strip()
        except Exception:
            clean = ""
        if clean and clean.lower() != name.lower():
            r = self.con.execute(
                "SELECT * FROM schemes WHERE fund_name=? COLLATE NOCASE LIMIT 1", (clean,)).fetchone()
            if r:
                return self._scheme_dict(r)
            r = self.con.execute(
                "SELECT * FROM schemes WHERE fund_name LIKE ? "
                "AND coverage='has_holdings' LIMIT 1", (f"%{clean}%",)).fetchone()
            if r:
                return self._scheme_dict(r)
        r = self.con.execute(
            "SELECT * FROM schemes WHERE fund_name LIKE ? "
            "AND coverage='has_holdings' LIMIT 1", (f"%{name}%",)).fetchone()
        if r:
            return self._scheme_dict(r)
        # Final fallback: canonical fuzzy match that tolerates 'and'/'&'
        # synonyms, punctuation/spacing differences and brand prefixes — e.g. a
        # CAS name 'ICICI Prudential Banking and PSU Debt Fund - Growth' resolves
        # to the stored 'ICICI Prudential Banking & PSU Debt Fund'. Only
        # schemes with holdings are considered, so this never "resolves" a
        # scheme that has no data on record.
        try:
            target = canon_name(clean or name).replace("and", "")
        except Exception:
            target = ""
        if target:
            for ckey, crow in self._canon_schemes():
                if ckey == target:
                    return self._scheme_dict(crow)
        return None

    def _canon_schemes(self) -> list[tuple[str, dict]]:
        """Lazy canonical index of schemes-with-holdings: canon_name(fund) with
        the conjunction word dropped so 'and' == '&' in fund names."""
        if self._canon is None:
            rows = self.con.execute(
                "SELECT * FROM schemes WHERE coverage='has_holdings'").fetchall()
            self._canon = [(canon_name(r["fund_name"]).replace("and", ""), dict(r))
                           for r in rows]
        return self._canon

    def resolve_scheme_ids(self, items: list[dict]) -> list[int]:
        """Distinct scheme ids for portfolio items, resolving items that only
        carry an ISIN/name (e.g. a CAS statement) via the scheme matcher."""
        ids: list[int] = []
        for it in items or []:
            if (it.get("type") or "").lower() not in ("scheme", "fund", "mf"):
                continue
            sid = it.get("id")
            if sid:
                try:
                    sid = int(sid)
                except (TypeError, ValueError):
                    sid = None
            if not sid:
                s = self._resolve_scheme_item(it)
                sid = s["id"] if s else None
            if sid and sid not in ids:
                ids.append(sid)
        return ids

    def _resolve_fund_unit(self, h: dict) -> dict | None:
        """Resolve a mutual-fund-unit holding to a scheme that has holdings, so
        fund-of-fund / arbitrage carry positions can be looked through. Many
        parsed holdings carry only an ISIN (no company text) — backfill the
        name from the securities directory first."""
        isin = (h.get("isin") or "").strip().upper()
        name = (h.get("company") or "").strip()
        if not name and isin:
            sec = self.get_security(isin)
            if sec and sec.get("name"):
                name = sec["name"]
        s = self._resolve_scheme_item({"isin": isin, "name": name})
        if not s or (s.get("coverage") or "has_holdings") != "has_holdings":
            return None
        return s

    def _resolve_stock_item(self, item: dict) -> dict | None:
        isin = (item.get("isin") or "").strip().upper()
        if isin:
            sec = self.get_security(isin)
            if sec:
                return sec
        name = (item.get("name") or "").strip()
        if not name:
            return None
        r = self.con.execute(
            "SELECT * FROM securities WHERE name=? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
        if not r:
            r = self.con.execute(
                "SELECT * FROM securities WHERE aliases LIKE ? OR name LIKE ? LIMIT 1",
                (f"%{name}%", f"%{name}%")).fetchone()
        return dict(r) if r else None

    def portfolio_analysis(self, items: list[dict]) -> dict:
        """Aggregate a weighted portfolio of schemes + direct stocks into an
        effective holding-level view (concentration, asset split, top holdings).

        Each item: {type: 'scheme'|'stock', id|name|isin, weight: pct-of-portfolio}.
        """
        schemes: list[dict] = []
        stocks: list[dict] = []
        errors: list[dict] = []
        effective: dict[str, dict] = {}
        total_weight = 0.0

        def _bump(key: str, meta: dict, contrib: float, src_scheme: str | None = None) -> None:
            e = effective.get(key)
            if e is None:
                e = {
                    "company": meta.get("company", ""), "isin": meta.get("isin", ""),
                    "sector": meta.get("sector", ""), "cap": meta.get("cap", "na"),
                    "asset_class": meta.get("asset_class", ""), "weight": 0.0,
                    "yield": meta.get("yield"), "rating": meta.get("rating"),
                    "coupon": meta.get("coupon"), "maturity_date": meta.get("maturity_date"),
                    "section": meta.get("section", ""),
                    "schemes": set(), "by_scheme": {}}
                effective[key] = e
            if src_scheme:
                e["schemes"].add(src_scheme)
                e["by_scheme"][src_scheme] = e["by_scheme"].get(src_scheme, 0.0) + contrib
            e["weight"] += contrib
            for k in ("yield", "rating", "coupon", "maturity_date"):
                if e.get(k) in (None, "") and meta.get(k) not in (None, ""):
                    e[k] = meta[k]

        def _is_fund_unit(h) -> bool:
            return (h.get("isin") or "").upper().startswith("INF")

        _MAX_LOOKTHROUGH_DEPTH = 10

        def _expand_holdings(sid: int, weight: float, src_scheme: str,
                             visited: set, depth: int) -> None:
            """Expand a scheme's holdings, looking through mutual-fund units
            (e.g. money-market funds inside arbitrage funds) into their own
            underlying instruments so no holding stays an opaque fund unit."""
            hs = self.scheme_holdings(sid)
            pct_rows = [(h, h.get("percent_nav")) for h in hs if h.get("percent_nav") is not None]
            total_pct = sum(p for _, p in pct_rows)
            factor = 100.0 / total_pct if total_pct > 0 else 0.0
            for h, pct in pct_rows:
                contrib = weight * (pct * factor) / 100.0
                key = (h.get("isin") or "").strip() or (h.get("company") or "").strip()
                if not key:
                    continue
                sub = None
                if depth < _MAX_LOOKTHROUGH_DEPTH and _is_fund_unit(h):
                    cand = self._resolve_fund_unit(h)
                    if cand and cand["id"] not in visited:
                        sub = cand
                if sub:
                    _expand_holdings(sub["id"], contrib, src_scheme,
                                     visited | {sub["id"]}, depth + 1)
                else:
                    _bump(key, {
                        "company": h.get("company", ""), "isin": h.get("isin", ""),
                        "sector": h.get("sector", ""), "cap": h.get("cap", "na"),
                        "asset_class": h.get("asset_class", ""),
                        "yield": h.get("yield") or h.get("ytm"),
                        "rating": h.get("rating"), "coupon": h.get("coupon"),
                        "maturity_date": h.get("maturity_date"),
                        "section": h.get("section", "")},
                        contrib, src_scheme=src_scheme)

        for item in items or []:
            try:
                weight = float(item.get("weight") or 0)
            except (TypeError, ValueError):
                errors.append({**item, "error": "invalid weight"})
                continue
            if weight <= 0:
                errors.append({**item, "error": "weight must be > 0"})
                continue
            total_weight += weight
            itype = (item.get("type") or "").strip().lower()

            if itype in ("scheme", "fund", "mf"):
                s = self._resolve_scheme_item(item)
                if not s:
                    errors.append({**item, "error": "scheme not found"})
                    continue
                schemes.append({
                    "id": s["id"], "fund_name": s["fund_name"], "amc": s["amc"],
                    "category": s.get("category"), "coverage": s.get("coverage"),
                    "weight": weight})
                _expand_holdings(s["id"], weight, s["fund_name"], {s["id"]}, 0)
            elif itype in ("stock", "equity"):
                sec = self._resolve_stock_item(item)
                if not sec:
                    errors.append({**item, "error": "stock not found"})
                    continue
                stocks.append({
                    "isin": sec.get("isin", ""), "name": sec.get("name", ""),
                    "cap": sec.get("cap", "na"), "sector": sec.get("sector", ""),
                    "weight": weight})
                key = (sec.get("isin") or "").strip() or (item.get("name") or "").strip()
                _bump(key, {
                    "company": sec.get("name", ""), "isin": sec.get("isin", ""),
                    "sector": sec.get("sector", ""), "cap": sec.get("cap", "na"),
                    "asset_class": "stocks"}, weight)
            else:
                errors.append({**item, "error": f"unknown type '{itype}'"})

        # Backfill missing company labels from the securities directory so charts
        # and tables never show a blank name (parsed debt/equity rows often carry
        # an ISIN but no company text).
        for key, e in effective.items():
            if not e.get("company") and e.get("isin"):
                sec = self.get_security(e["isin"])
                if sec and sec.get("name"):
                    e["company"] = sec["name"]

        holdings = sorted(effective.values(), key=lambda x: -x["weight"])
        top = holdings[:10]
        others = round(sum(h["weight"] for h in holdings[10:]), 4)
        effective_total = round(sum(h["weight"] for h in holdings), 2)
        coverage_pct = round(effective_total / total_weight * 100, 1) if total_weight else 0.0

        gold_re = re.compile(r"\\bgold\\b", re.I)
        rating_re = re.compile(r"(?:AAA|AA\+|AA|A1\+|BBB|A\+|SOV|SOVEREIGN)", re.I)
        debt_text_re = re.compile(
            r"(government security|state government|g-sec|gsec|sovereign|treasury|"
            r"t-bill|tbill|\bsdl\b|\bgoi\b|net current assets|net receivables)", re.I)
        asset_split: dict[str, float] = {}
        sector_split: dict[str, float] = {}
        cap_split: dict[str, float] = {}
        _cap_labels = {"large": "large cap", "mid": "mid cap", "small": "small cap",
                       "microcap": "microcap", "sme": "microcap"}
        for h in holdings:
            ac = h.get("asset_class") or "other"
            company = h.get("company") or ""
            sector = h.get("sector") or ""
            if gold_re.search(company) or gold_re.search(sector):
                ac = "gold"
            # Reclassify government securities / rated bonds that fell into
            # "other" as debt, so the asset-allocation pie has no bare "Other" slice.
            elif ac in ("other", "") and (rating_re.search(sector)
                                          or debt_text_re.search(company + " " + sector)):
                ac = "debt"
                h["asset_class"] = "debt"
            elif ac == "stocks":
                # Arbitrage funds park in money-market / liquid / savings fund
                # units (cash-like carry) — those are NOT equity exposure, and
                # untagged securities carrying a credit rating are short-term
                # debt (bank CDs / CP / bonds) that slipped into the equity tag.
                # Reclassify so the equity sleeve matches the equity cap split.
                isin_up = (h.get("isin") or "").upper()
                if isin_up.startswith("INF"):
                    if re.search(r"(money market|liquid|savings|overnight|low duration|"
                                 r"ultra short|tbill|t-bill|treasury)", company, re.I):
                        ac = "cash_equivalents"
                    else:
                        ac = "debt"
                elif (h.get("cap") or "na") == "na" and rating_re.search(sector):
                    ac = "debt"
                if ac != "stocks":
                    h["asset_class"] = ac
            asset_split[ac] = asset_split.get(ac, 0.0) + h["weight"]
            sec = (sector or "na").strip() or "na"
            # Sector concentration is an EQUITY-industry concept: debt holdings
            # carry credit ratings (CRISIL AAA / SOV …), not industry sectors,
            # so only equity holdings feed the sector split.
            if ac == "stocks":
                sector_split[sec] = sector_split.get(sec, 0.0) + h["weight"]
            # Equity cap split: only the known market-cap buckets of equity
            # holdings (no Debt / Fund units / Unclassified segments).
            if ac == "stocks":
                cap_key = _cap_bucket(h)
                cap_label = _cap_labels.get(cap_key, cap_key)
                if cap_key in ("large", "mid", "small", "microcap", "ipo"):
                    cap_split[cap_label] = cap_split.get(cap_label, 0.0) + h["weight"]

        # Reconcile the cap split with the Equity slice: distribute untagged
        # equity (REITs / untagged stocks) proportionally across the tagged
        # buckets so the cap split sums to the same total as Equity.
        _equity_total = asset_split.get("stocks", 0.0)
        _tagged_total = sum(cap_split.values())
        if _tagged_total > 0 and _equity_total > _tagged_total * 1.0001:
            _scale = _equity_total / _tagged_total
            cap_split = {k: round(v * _scale, 4) for k, v in cap_split.items()}

        alloc_labels = {"stocks": "Equity", "debt": "Debt", "gold": "Gold",
                        "international": "International", "cash_equivalents": "Cash",
                        "future_options": "Futures & Options", "other": "Other"}
        asset_chart = [{"label": alloc_labels.get(k, k), "value": round(v, 2)}
                       for k, v in sorted(asset_split.items(), key=lambda x: -x[1])]
        asset_split_raw = {k: round(v, 2) for k, v in asset_split.items()}

        cap_labels = {"large cap": "Large Cap", "mid cap": "Mid Cap",
                      "small cap": "Small Cap", "microcap": "Microcap",
                      "ipo": "IPO", "debt": "Debt instruments",
                      "fund units": "Fund units", "unclassified": "Unclassified"}
        cap_chart = [{"label": cap_labels.get(k, k), "value": round(v, 2)}
                     for k, v in sorted(cap_split.items(), key=lambda x: -x[1])]
        cap_split_raw = {k: round(v, 2) for k, v in cap_split.items()}

        # Which funds contribute to each equity cap segment, and which funds make up debt.
        cap_schemes: dict[str, set] = {}
        debt_schemes: set = set()
        for h in holdings:
            src_schemes = h.get("schemes") or set()
            if src_schemes:
                if (h.get("asset_class") or "") == "stocks":
                    cap_key = _cap_bucket(h)
                    cap_label = _cap_labels.get(cap_key, cap_key)
                    if cap_key in ("large", "mid", "small", "microcap", "ipo"):
                        cap_schemes.setdefault(cap_label, set()).update(src_schemes)
                if (h.get("asset_class") or "") == "debt":
                    debt_schemes.update(src_schemes)
        cap_schemes_out = {k: sorted(v) for k, v in cap_schemes.items()}

        # Sector concentration: top 8 sectors + an "Others" slice for the pie.
        sector_sorted = sorted(sector_split.items(), key=lambda x: -x[1])
        sector_chart = [{"label": k, "value": round(v, 2)} for k, v in sector_sorted[:8]]
        rest = round(sum(v for _, v in sector_sorted[8:]), 2)
        if rest > 0:
            sector_chart.append({"label": "Others", "value": rest})
        sector_table = [{"sector": k, "weight": round(v, 2)} for k, v in sector_sorted]

        top_pcts = [round(h["weight"], 2) for h in top]
        concentration = {
            "top_holding": top[0] if top else None,
            "top1_pct": top_pcts[0] if top_pcts else 0.0,
            "top5_pct": round(sum(top_pcts[:5]), 2),
            "top10_pct": round(sum(top_pcts[:10]), 2),
        }

        debt_analysis = self._debt_analysis(holdings, asset_split)

        return {
            "schemes": schemes,
            "stocks": stocks,
            "total_weight": round(total_weight, 2),
            "effective_total": effective_total,
            "coverage_pct": coverage_pct,
            "n_holdings": len(holdings),
            "effective_holdings": holdings,
            "top_holdings": [{"label": h["company"], "value": round(h["weight"], 2),
                              "isin": h.get("isin", ""), "sector": h.get("sector", ""),
                              "asset_class": h.get("asset_class", "")} for h in top],
            "others_pct": others,
            "asset_split": asset_chart,
            "asset_split_raw": asset_split_raw,
            "cap_split": cap_chart,
            "cap_split_raw": cap_split_raw,
            "cap_schemes": cap_schemes_out,
            "debt_schemes": sorted(debt_schemes),
            "sector_split": sector_chart,
            "sector_table": sector_table,
            "concentration": concentration,
            "debt_analysis": debt_analysis,
            "errors": errors,
        }

    def _pct_or_none(self, v):
        """Numeric value as a percent; normalises fractions (0.0741 -> 7.41)."""
        f = _num(v)
        if f is None:
            return None
        return f * 100 if 0 < f < 1 else f

    def _debt_analysis(self, holdings: list, asset_split: dict) -> dict:
        """Debt-portfolio metrics from the effective holdings.

        YTM per bond is first the fund's own yield text, then the bond-market
        catalog's reported yield, then a YTM COMPUTED from coupon + last-trade
        price + maturity (src.bonds) — so model-portfolio debt analysis always
        carries the best available yield. Same backfill for coupon/rating/
        maturity so the credit and maturity buckets are complete."""
        debt_holdings = [h for h in holdings if (h.get("asset_class") or "") == "debt"]
        out = {"debt_pct": round(asset_split.get("debt", 0.0), 2),
               "n_debt_holdings": len(debt_holdings)}

        y_h = [(h["weight"], self._holding_ytm_pct(h)) for h in debt_holdings]
        y_h = [(w, y) for w, y in y_h if y is not None]
        out["ytm_pct"] = (round(sum(w * y for w, y in y_h) / sum(w for w, _ in y_h), 2)
                          if y_h else None)
        out["ytm_cover"] = (round(len(y_h) / len(debt_holdings) * 100, 1)
                            if debt_holdings else None)

        mat = []
        for h in debt_holdings:
            m = h.get("maturity_date")
            if not m:
                continue
            try:
                mat_d = datetime.strptime(str(m)[:10], "%Y-%m-%d").date()
                yrs = (mat_d - datetime.now().date()).days / 365.0
                if -1 <= yrs <= 60:
                    mat.append((h["weight"], round(yrs, 2)))
            except Exception:
                continue
        out["avg_maturity_yrs"] = (round(sum(w * y for w, y in mat) / sum(w for w, _ in mat), 2)
                                   if mat else None)

        credit: dict[str, float] = {}
        instr: dict[str, float] = {}
        for h in debt_holdings:
            sect = h.get("section") or ""
            cb = _credit_bucket(h.get("rating"), h.get("sector") or "", sect,
                                h.get("company") or "")
            credit[cb] = credit.get(cb, 0.0) + h["weight"]
            ib = _instrument_type(h.get("company") or "", h.get("sector") or "", sect)
            instr[ib] = instr.get(ib, 0.0) + h["weight"]
        out["credit_split"] = [{"label": k, "value": round(v, 2)}
                               for k, v in sorted(credit.items(), key=lambda x: -x[1])]
        out["instrument_split"] = [{"label": k, "value": round(v, 2)}
                                   for k, v in sorted(instr.items(), key=lambda x: -x[1])]
        out["top_debt_holdings"] = [{
            "company": h.get("company", ""), "isin": h.get("isin", ""),
            "rating": h.get("rating") or "", "yield": self._holding_ytm_pct(h),
            "maturity_date": h.get("maturity_date"),
            "coupon": h.get("coupon"),
            "ytm_source": h.get("ytm_source") or "",
            "weight": round(h["weight"], 2)}
            for h in sorted(debt_holdings, key=lambda x: -x["weight"])[:8]]
        return out

    # ---- mapping search ----
    def mapping(self, q: str, limit=50) -> list[dict]:
        nq = norm_name(q)
        out = []
        if not q:
            return out
        seen = set()
        for h in self.con.execute(
            """SELECT h.company, h.isin, h.fund_name, h.amc, h.sector, h.as_of
               FROM holdings h WHERE h.company LIKE ? OR h.isin LIKE ? OR h.isin=?
               ORDER BY h.percent_nav DESC NULLS LAST LIMIT ?""",
            (f"%{q}%", f"%{q.upper()}%", q.upper(), limit)).fetchall():
            d = dict(h)
            if d["isin"] and d["isin"] in seen:
                continue
            if d["isin"]:
                seen.add(d["isin"])
            sec = self.get_security(d["isin"]) if d["isin"] else None
            out.append({
                "company": d["company"], "isin": d["isin"],
                "confirmed_equity": sec["confirmed_equity"] if sec else None,
                "cap": sec["cap"] if sec else "na",
                "sector": sec["sector"] if sec else d["sector"],
                "scheme": d["fund_name"], "amc": d["amc"], "as_of": d["as_of"],
            })
            if len(out) >= limit:
                break
        return out

    def close(self):
        self.con.close()


def get_db(force: bool = False) -> WebDB:
    build_db(force=force)
    return WebDB()