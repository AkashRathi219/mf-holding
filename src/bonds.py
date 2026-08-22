"""NSE bond-market data: daily bulk-file ingestion, live-API fallback, YTM math.

Sources (direct HTML scraping of nseindia.com is not attempted because
Akamai/Cloudflare blocks it):

PRIMARY - daily bulk files (nsearchives.nseindia.com, no anti-bot protection):
  1. CBM "Security Master" (corporate bond market report, ~3,700 bonds):
     https://nsearchives.nseindia.com/content/debt/Corporate_bond_report_{DD-Mon-YYYY}.csv
  2. WDM "Securities available for trading" (G-Secs, SDLs, T-Bills, PSU bonds):
     https://nsearchives.nseindia.com/content/historical/WDM/{YYYY}/{MON}/wdmlist_{DDMMYYYY}.csv
  3. CBM daily trades (per-ISIN last-trade price & yield):
     https://nsearchives.nseindia.com/archives/debt/cbm/cbm_trd{YYYYMMDD}.csv

SECONDARY - live JSON endpoint (best-effort; cookie handshake + spoofed
headers; Akamai may return 403 from datacenter IPs, in which case the bulk
files are the fallback):
  https://www.nseindia.com/api/live-analysis-debt-market

Local seeds (gap-fill coupon/maturity for ISINs missing from the dumps):
  data/nifty/debt_constituents/*.csv   (ISIN + name + issuer + weights)
  data/nifty/debt_constituents/zerodha_debt_fund_holdings_31-jul-26.csv
  data/reference/index_resolved_holdings.json

Outputs:
  data/bond_market/raw/<YYYY-MM-DD>/    cached downloads
  data/bond_market/live_debt_market.json  live snapshot (when reachable)
  data/reference/bonds_catalog.json     merged, YTM-computed bond universe
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from src.stock_common import http_get, make_opener

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
BOND_DIR = BASE_DIR / "data" / "bond_market"
RAW_DIR = BOND_DIR / "raw"
LIVE_JSON = BOND_DIR / "live_debt_market.json"
CATALOG_JSON = BASE_DIR / "data" / "reference" / "bonds_catalog.json"

NIFTY_DEBT_DIR = BASE_DIR / "data" / "nifty" / "debt_constituents"
ZERODHA_DEBT_HOLDINGS = NIFTY_DEBT_DIR / "zerodha_debt_fund_holdings_31-jul-26.csv"

NSE_HOME = "https://www.nseindia.com/"
LIVE_DEBT_API = "https://www.nseindia.com/api/live-analysis-debt-market"


# --------------------------------------------------------------------------
# URL builders
# --------------------------------------------------------------------------

def corp_master_url(d: date) -> str:
    return (f"https://nsearchives.nseindia.com/content/debt/"
            f"Corporate_bond_report_{d.strftime('%d-%b-%Y')}.csv")


def wdm_url(d: date) -> str:
    return (f"https://nsearchives.nseindia.com/content/historical/WDM/"
            f"{d.strftime('%Y')}/{d.strftime('%b').upper()}/"
            f"wdmlist_{d.strftime('%d%m%Y')}.csv")


def cbm_trades_url(d: date) -> str:
    return (f"https://nsearchives.nseindia.com/archives/debt/cbm/"
            f"cbm_trd{d.strftime('%Y%m%d')}.csv")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 40) -> bytes | None:
    """GET with retries; returns None on 404/network failure (daily bulk files
    only exist for trading days, so misses are normal)."""
    try:
        return http_get(url, headers={"Referer": "https://www.nseindia.com/"},
                        timeout=timeout)
    except Exception as e:  # noqa: BLE001 - bulk-file misses are expected
        if "HTTP Error 404" in str(e):
            return None
        print(f"  ! {url} -> {e}")
        return None


def fetch_day(d: date) -> dict:
    """Download all bulk files for ``d``; returns {name: path} for the files
    that arrived (empty dict when the day is not a trading day)."""
    out: dict[str, Path] = {}
    dstr = d.strftime("%Y-%m-%d")
    target = RAW_DIR / dstr
    target.mkdir(parents=True, exist_ok=True)
    jobs = {
        f"corp_master_{dstr}.csv": corp_master_url(d),
        f"wdm_list_{dstr}.csv": wdm_url(d),
        f"cbm_trades_{dstr}.csv": cbm_trades_url(d),
    }
    for name, url in jobs.items():
        path = target / name
        if path.exists() and path.stat().st_size > 0:
            out[name] = path
            continue
        data = _fetch(url)
        if data is None:
            continue
        path.write_bytes(data)
        out[name] = path
        time.sleep(0.25)
    return out


def fetch_live_debt_market() -> list[dict]:
    """Best-effort pull of the live debt-market JSON API with a warm-up cookie
    handshake. Returns a normalized list of records; [] when unreachable."""
    try:
        opener = make_opener(cookies=True)
        hdrs = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Cookie handshake: visiting the home page sets the session cookies.
        try:
            opener.open(urllib_request(NSE_HOME, hdrs), timeout=30)
        except Exception:
            pass
        api_hdrs = dict(hdrs)
        api_hdrs["Referer"] = "https://www.nseindia.com/market-data/live-analysis-debt-market"
        raw = http_get(LIVE_DEBT_API, headers=api_hdrs, opener=opener, timeout=30)
        data = json.loads(raw.decode("utf-8"))
        records = _normalize_live_payload(data)
        if not records:
            return []
        idx = RAW_DIR / "live" / datetime.now().strftime("%Y-%m-%d")
        idx.mkdir(parents=True, exist_ok=True)
        LIVE_JSON.write_text(json.dumps(
            {"fetched_at": datetime.now().isoformat(timespec="seconds"),
             "source": "live-analysis-debt-market",
             "records": records}, ensure_ascii=False, indent=1), encoding="utf-8")
        return records
    except Exception as e:  # noqa: BLE001
        print(f"  ! live API unavailable ({e}); using bulk files only.")
        return []


def urllib_request(url: str, headers: dict):
    import urllib.request
    return urllib.request.Request(url, headers=headers)


_LIVE_KEY_ALIASES = {
    "isin": ["isin", "ISIN", "securityISIN", "securityIsin"],
    "name": ["name", "AN", "security_name", "securityName", "desc", "security"],
    "coupon": ["coupon", "couponRate", "interest", "rate"],
    "maturity_date": ["maturity", "maturityDate", "maturity_date", "matDate"],
    "price": ["price", "lastPrice", "last_price", "px", "close"],
    "ytm": ["ytm", "yield", "YTM", "lastYield"],
    "issuer": ["issuer", "AN", "issuerName"],
    "rating": ["rating", "creditRating"],
    "segment": ["segment", "sector", "type"],
}


def _normalize_live_payload(data) -> list[dict]:
    """Flatten whatever shape the live API returns into a record list."""
    candidates: list[dict] = []
    def walk(node, depth: int = 0):
        if depth > 6 or not isinstance(node, dict):
            return
        if len(candidates) > 8000:
            return
        keys = set(str(k).lower() for k in node.keys())
        if any(k in keys for k in ("isin", "ISIN", "securityISIN")):
            rec: dict = {}
            for out_key, aliases in _LIVE_KEY_ALIASES.items():
                for a in aliases:
                    if a in node:
                        rec[out_key] = node[a]
                        break
            if rec.get("isin"):
                candidates.append(rec)
            return
        for v in node.values():
            walk(v, depth + 1)
    walk(data)
    return candidates


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

_MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_COUPON_RE = re.compile(r"(\d{1,2}(?:\.\d{1,4})?)\s*%")
_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _iso_date(v) -> str | None:
    """'13-Dec-2032' / '28-Aug-2026' / ISO -> 'YYYY-MM-DD'; None if junk."""
    s = (str(v or "")).strip()
    if not s:
        return None
    m = _DATE_RE.search(s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3])
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(1)):02d}"
    m = _ISO_DATE_RE.search(s)
    if m:
        return f"{int(m.group(1)):04d}-{m.group(2)}-{m.group(3)}"
    return None


def _num(v) -> float | None:
    s = do_number_text(v)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def do_number_text(v) -> str | None:
    s = (str(v or "")).replace(",", "").replace("₹", "").strip()
    if not s or s.lower() in ("na", "n/a", "-", "0", "0.0", "0.00", "0.0000"):
        return None
    m = _FLOAT_RE.search(s)
    return m.group(0) if m else None


def _coupon_from(*parts) -> float | None:
    """Coupon % from an issue name / description; None for floaters (the rate is
    a floating benchmark, not a coupon) and undated strings."""
    for p in parts:
        t = str(p or "")
        if not t:
            continue
        low = t.lower()
        if any(k in low for k in ("frb", "floating", "floating rate", "frn")):
            return None
        m = _COUPON_RE.search(t)
        if m:
            return float(m.group(1))
    return None


def _freq_per_year(coupon_freq) -> int | None:
    f = str(coupon_freq or "").strip().lower()
    if not f or f in ("0", "none"):
        return None
    if "half" in f or "semi" in f or f.startswith("2"):
        return 2
    if "quart" in f or f.startswith("4"):
        return 4
    if "month" in f or f.startswith("12"):
        return 12
    if "year" in f or "annual" in f or f.startswith("1"):
        return 1
    if f.startswith("6"):
        return 6
    return None


# --------------------------------------------------------------------------
# YTM math
# --------------------------------------------------------------------------

_DEFAULT_SETTLE: str | None = None


def months_to_maturity(maturity: str | None, settle: str | None = None) -> float | None:
    """Calendar days to maturity (None when already matured / undated)."""
    m = _iso_date(maturity)
    if not m:
        return None
    today = settle or date.today().isoformat()
    mdt = datetime.strptime(m, "%Y-%m-%d")
    st = datetime.strptime(today[:10], "%Y-%m-%d")
    days = (mdt.date() - st.date()).days
    if days <= 0:
        return None
    return days


def compute_ytm(coupon: float | None, price: float | None,
                maturity: str | None, settle: str | None = None,
                freq: int | None = 2) -> float | None:
    """Annualized YTM (%) for a bond, solved from the standard price equation.

    P = sum_{k=1..n} (C/f)/(1+y/f)^k + F/(1+y/f)^n

    Zero-coupon money-market instruments (T-Bills: no coupon) use the
    annualized money-market yield instead. Returns None when a price is not
    credible (<= 0 or > 300) or the bond is already matured.
    """
    p = _num(price)
    if p is None or p <= 0 or p > 300:
        return None
    if freq is None:
        freq = 1
    c = _num(coupon)
    days = months_to_maturity(maturity, settle)
    if days is None:
        return None
    years = days / 365.25
    if c is None or c <= 0:
        # money-market zero-coupon annualization
        if years <= 0:
            return None
        try:
            return (round((100.0 / p) ** (1.0 / years) - 1.0, 6) * 100.0)
        except (OverflowError, ZeroDivisionError):
            return None
    n = years * freq
    frac = n - math.floor(n)
    if frac < 1e-9:
        # settlement on a coupon date: next coupon is one full period away
        w, m = 1.0, int(round(n))
    else:
        w, m = frac, int(math.floor(n)) + 1
    if m < 1:
        m = 1
    # accrued interest since the last coupon (fraction of period × coupon rate)
    accrued = (1.0 - w) * (c / freq)

    def price_at(y: float) -> float:
        # CLEAN price per the standard fractional-period convention (Excel
        # PRICE / Street): dirty PV of m coupons at w..w+m-1 and face (plus the
        # final coupon already in the m payments) at w+m-1, minus accrued.
        cf = (c / 2.0 if freq == 2 else c / freq)
        mkt = 1.0 + y / freq
        total = 0.0
        for k in range(m):
            total += cf / (mkt ** (w + k))
        total += 100.0 / (mkt ** (w + (m - 1)))
        return total - accrued

    # bisection on y in [1e-6, 6] (0.00001% .. 600%)
    lo, hi = 1e-6, 6.0
    try:
        if price_at(lo) < p:
            return None  # price above max theoretical PV - odd data
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if price_at(mid) > p:
                lo = mid
            else:
                hi = mid
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    return round(mid * 100.0, 6)


def current_yield(coupon: float | None, price: float | None) -> float | None:
    c, p = _num(coupon), _num(price)
    if c is None or p is None or p <= 0:
        return None
    return c / p * 100.0


def resolve_ytm(rec: dict) -> dict:
    """Fill ``rec['ytm']`` (and ``ytm_source``) from reported yields first,
    then a computed YTM from coupon + price + maturity."""
    last_y = _num(rec.get("last_yield"))
    wa_y = _num(rec.get("wa_yield"))
    if last_y is not None and last_y > 0:
        rec["ytm"], rec["ytm_source"] = round(last_y, 4), "reported (last trade)"
    elif wa_y is not None and wa_y > 0:
        rec["ytm"], rec["ytm_source"] = round(wa_y, 4), "reported (weighted avg)"
    else:
        done = _ytm_from_price(rec)
        if done is not None:
            rec["ytm"], rec["ytm_source"] = done
        else:
            cy = current_yield(rec.get("coupon"), rec.get("price"))
            if cy is not None:
                rec["ytm"], rec["ytm_source"] = round(cy, 4), "current yield (no maturity/price data)"
            else:
                rec["ytm"], rec["ytm_source"] = None, "n/a"
    return rec


def _ytm_from_price(rec: dict) -> tuple[float, str] | None:
    price = _num(rec.get("price")) or _num(rec.get("last_price"))
    if price is None or price <= 0:
        return None
    freq = _freq_per_year(rec.get("coupon_freq"))
    seg = rec.get("segment") or ""
    freq = freq or (1 if seg == "T-Bill" else 2)
    # Price/yield pairs are quoted at the LAST TRADE, so solve the YTM with the
    # last-trade date as settlement (today's day-count on an old price produces
    # an explosive/meaningless yield near maturity).
    settle = rec.get("last_trade_date") or rec.get("trade_date") or None
    y = compute_ytm(rec.get("coupon"), price, rec.get("maturity_date"),
                    settle=settle, freq=freq)
    if y is None:
        return None
    kind = ("money-market zero-coupon" if (_num(rec.get("coupon")) or 0) <= 0
            else "computed from coupon+price+maturity")
    return round(y, 4), f"computed: {kind}"


# --------------------------------------------------------------------------
# Bulk-file parsers
# --------------------------------------------------------------------------

def _rows_from_csv(path: Path, skip_until: str | None = None) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = [list(r) for r in csv.reader(fh)]
    if skip_until:
        for i, r in enumerate(rows):
            if r and r[0].strip().lower() == skip_until.lower():
                return rows[i + 1:]
        return []
    return rows


_CORP_COLS = ["sectype", "security", "issue_name", "issue_desc", "issuer",
              "face_value", "credit_rating", "issue_date", "maturity_date",
              "record_date", "step_up_coupons", "coupon_freq", "next_coupon_date",
              "day_count_convention", "floating_benchmark", "spread_over_benchmark",
              "last_trade_date", "last_price", "last_trade_value_lakhs",
              "last_yield", "wa_price", "wa_yield", "traded_value_cr",
              "isin", "status"]


def parse_corp_master(path: Path) -> list[dict]:
    """CBM 'Security Master' (Corporate_bond_report_*.csv)."""
    out: list[dict] = []
    for r in _rows_from_csv(path, "sectype"):
        if len(r) < 25:
            continue
        d = dict(zip(_CORP_COLS, r[:25]))
        isin = (d.get("isin") or "").strip().upper()
        if not re.match(r"^IN[A-Z0-9]{10}$", isin):
            continue
        name = (d.get("issue_desc") or d.get("issue_name") or "").strip()
        seg = _segment_from_sectype(d.get("sectype"))
        rec = {
            "isin": isin,
            "name": name or (d.get("issue_name") or "").strip(),
            "coupon": _coupon_from(d.get("issue_name"), d.get("issue_desc"), name),
            "coupon_label": (d.get("issue_name") or "").strip(),
            "issuer": (d.get("issuer") or "").strip(),
            "segment": seg,
            "sectype": d.get("sectype") or "",
            "rating": (d.get("credit_rating") or "").strip(),
            "face_value": _num(d.get("face_value")),
            "issue_date": _iso_date(d.get("issue_date")),
            "maturity_date": _iso_date(d.get("maturity_date")),
            "coupon_freq": (d.get("coupon_freq") or "").strip(),
            "last_trade_date": _iso_date(d.get("last_trade_date")),
            "last_price": _num(d.get("last_price")),
            "last_trade_value_lakhs": _num(d.get("last_trade_value_lakhs")),
            "last_yield": _num(d.get("last_yield")),
            "wa_price": _num(d.get("wa_price")),
            "wa_yield": _num(d.get("wa_yield")),
            "traded_value_cr": _num(d.get("traded_value_cr")),
            "status": (d.get("status") or "").strip(),
            "source": "CBM security master",
        }
        _finalize_rec(rec)
        out.append(rec)
    return out


_WDM_COLS = ["sectype", "security", "issue_name", "issue_desc", "issue_date",
             "mat_date", "last_ip_dt", "next_ip_dt", "cpn_freq",
             "last_traded_date", "last_traded_price", "isin", "status"]


def parse_wdm(path: Path) -> list[dict]:
    """WDM 'Securities available for trading' (wdmlist_*.csv)."""
    out: list[dict] = []
    for r in _rows_from_csv(path, "sectype"):
        if len(r) < 13:
            continue
        d = dict(zip(_WDM_COLS, r[:13]))
        isin = (d.get("isin") or "").strip().upper()
        if not re.match(r"^IN[A-Z0-9]{10}$", isin):
            continue
        name = (d.get("issue_desc") or d.get("issue_name") or "").strip()
        seg = _segment_from_sectype(d.get("sectype"))
        rec = {
            "isin": isin,
            "name": name,
            "coupon": _coupon_from(d.get("issue_name"), d.get("issue_desc"), name),
            "coupon_label": (d.get("issue_name") or "").strip(),
            "issuer": _issuer_from_wdm(d.get("sectype"), name),
            "segment": seg,
            "sectype": d.get("sectype") or "",
            "rating": "",
            "face_value": 100.0,
            "issue_date": _iso_date(d.get("issue_date")),
            "maturity_date": _iso_date(d.get("mat_date")) or _maturity_in_name(name),
            "coupon_freq": (d.get("cpn_freq") or "").strip(),
            "last_trade_date": _iso_date(d.get("last_traded_date")),
            "last_price": _num(d.get("last_traded_price")),
            "last_trade_value_lakhs": None,
            "last_yield": None,
            "wa_price": None,
            "wa_yield": None,
            "traded_value_cr": None,
            "status": (d.get("status") or "").strip(),
            "source": "WDM securities list",
        }
        _finalize_rec(rec)
        out.append(rec)
    return out


_TRADE_COLS = ["trade_date", "isin", "last_price", "last_trade_value_lakhs",
               "total_trade_value_lakhs", "last_trade_yield",
               "wa_price", "wa_yield"]


def parse_cbm_trades(path: Path) -> list[dict]:
    """CBM daily trades (cbm_trd*.csv): ISIN -> last/wa price & yield per day."""
    out: list[dict] = []
    for r in _rows_from_csv(path, "trade date"):
        if len(r) < 7:
            continue
        d = dict(zip(_TRADE_COLS, r[:8]))
        isin = (d.get("isin") or "").strip().upper()
        if not re.match(r"^IN[A-Z0-9]{10}$", isin):
            continue
        out.append({
            "trade_date": _iso_date(d.get("trade_date")),
            "isin": isin,
            "last_price": _num(d.get("last_price")),
            "last_trade_value_lakhs": _num(d.get("last_trade_value_lakhs")),
            "total_trade_value_lakhs": _num(d.get("total_trade_value_lakhs")),
            "last_yield": _num(d.get("last_trade_yield")),
            "wa_price": _num(d.get("wa_price")),
            "wa_yield": _num(d.get("wa_yield")),
        })
    return out


# --------------------------------------------------------------------------
# Segment & name heuristics
# --------------------------------------------------------------------------

_SECTYPE_SEGMENT = {
    "GS": "G-Sec", "GZ": "G-Sec STRIPS", "GF": "G-Sec FRB", "PR": "G-Sec",
    "TB": "T-Bill",
    "SG": "State Govt / SDL",
    "PT": "PSU / Sub-sovereign",
    "CP": "Commercial Paper",
    "CD": "Certificate of Deposit",
    "PZ": "STRIPPED Debt",
    "VZ": "STRIPPED Debt",
}


def _segment_from_sectype(sectype: str) -> str:
    return _SECTYPE_SEGMENT.get((sectype or "").strip().upper(), "Corporate Bond")


def _issuer_from_wdm(sectype: str, name: str) -> str:
    seg = _segment_from_sectype(sectype)
    if seg in ("G-Sec", "G-Sec STRIPS", "G-Sec FRB", "T-Bill"):
        return "Government of India" if seg != "T-Bill" else "Government of India (T-Bill)"
    if seg == "State Govt / SDL":
        m = re.match(r"([A-Z][A-Z])", name or "")
        state = _STATE_CODES.get((m.group(1) if m else ""))
        return f"State Government ({state or 'SDL'})"
    return ""


_STATE_CODES = {
    "AN": "Andaman & Nicobar", "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh",
    "AS": "Assam", "BR": "Bihar", "CG": "Chhattisgarh", "CH": "Chandigarh",
    "DL": "Delhi", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh",
    "JK": "Jammu & Kashmir", "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala",
    "LA": "Ladakh", "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur",
    "ML": "Meghalaya", "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha",
    "PB": "Punjab", "PY": "Puducherry", "RJ": "Rajasthan", "SK": "Sikkim",
    "TN": "Tamil Nadu", "TS": "Telangana", "TR": "Tripura", "UP": "Uttar Pradesh",
    "UK": "Uttarakhand", "UT": "Uttarakhand", "WB": "West Bengal",
}


_MATURITY_IN_NAME_RE = re.compile(
    r"(?:(\d{2})[-\s]([A-Za-z]{3}[a-z]?)[-\s](\d{4}))"
    r"|\b(\d{4})\b")


def _maturity_in_name(name: str) -> str | None:
    """'6.48% GOI 06-Oct-2035' -> 2035-10-06; '7.04% GS 2029' -> 2029-12-31
    (year-only: pick Dec 31 of the stated year)."""
    s = str(name or "")
    m = re.search(r"(\d{1,2})-([A-Za-z]{3})[a-z]*-(\d{4})", s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3])
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(1)):02d}"
    if re.search(r"(?:GS|G-SEC|GOI|TBILL|T-BILL|SDL|LOAN|BOND)\b", s, re.I):
        y = re.findall(r"\b20(\d{2})\b", s)
        if y:
            return f"20{y[-1]}-12-31"
    return _iso_date(s)


def _finalize_rec(rec: dict) -> None:
    """Shared post-parse enrichment: price + days-to-maturity + YTM."""
    price = _num(rec.get("price")) or _num(rec.get("last_price"))
    rec["price"] = price
    settle = rec.get("last_trade_date") or rec.get("trade_date") or None
    days = months_to_maturity(rec.get("maturity_date"), settle)
    rec["days_to_maturity"] = round(days, 0) if days else None
    rec["yrs_to_maturity"] = round(days / 365.25, 2) if days else None
    resolve_ytm(rec)


def default_ytm_for(rec: dict) -> dict:
    """Public helper (used by the webapp): returns rec with resolved ytm."""
    return resolve_ytm(rec)


# --------------------------------------------------------------------------
# Local seed corpora (gap-fill for ISINs absent from the daily dumps)
# --------------------------------------------------------------------------

def _seed_records() -> list[dict]:
    """ISIN -> gap-fill record from the local Nifty/zerodha debt files."""
    out: dict[str, dict] = {}

    def add(rec: dict) -> None:
        isin = (rec.get("isin") or "").strip().upper()
        if not re.match(r"^IN[A-Z0-9]{10}$", isin):
            return
        prev = out.get(isin)
        if prev is None:
            out[isin] = rec
        else:
            # keep the first sheet order; fill missing fields only
            for k, v in rec.items():
                if v and not prev.get(k):
                    prev[k] = v

    if NIFTY_DEBT_DIR.is_dir():
        for p in sorted(NIFTY_DEBT_DIR.glob("*.csv")):
            if p.name.startswith("zerodha_dirf"):
                continue
            try:
                rows = _rows_from_csv(p)
            except Exception:
                continue
            if not rows:
                continue
            hdr = [str(h).strip().lower() for h in rows[0]]
            if "isin" not in hdr:
                continue
            idx = {h: i for i, h in enumerate(hdr)}
            def cell(r, *names):
                for n in names:
                    i = idx.get(n.lower())
                    if i is not None and i < len(r):
                        return r[i]
                return ""
            for r in rows[1:]:
                if len(r) < 2:
                    continue
                isin = cell(r, "isin", "isin no.',", "isin no", "scrip code")
                name = cell(r, "security name", "security_name", "name", "security",
                            "issue desc")
                issuer = cell(r, "issuer")
                maturity = cell(r, "maturity", "last maturity", "mat date", "mat_date")
                coupon = _coupon_from(name)
                add({
                    "isin": isin.strip().upper(),
                    "name": name.strip(),
                    "coupon": coupon,
                    "coupon_label": "",
                    "issuer": issuer.strip(),
                    "segment": _segment_from_text(name),
                    "sectype": "",
                    "rating": "",
                    "face_value": 100.0,
                    "issue_date": None,
                    "maturity_date": _iso_date(maturity) or _maturity_in_name(name),
                    "coupon_freq": cell(r, "coupon frequency", "cpn freq") or "",
                    "last_trade_date": None,
                    "last_price": None,
                    "last_trade_value_lakhs": None,
                    "last_yield": None,
                    "wa_price": None,
                    "wa_yield": None,
                    "traded_value_cr": None,
                    "status": "",
                    "source": f"local seed ({p.name})",
                })

    if ZERODHA_DEBT_HOLDINGS.exists():
        try:
            for r in _rows_from_csv(ZERODHA_DEBT_HOLDINGS):
                if len(r) < 3:
                    continue
                name = (r[2] if len(r) > 2 else "")
                maturity = r[3] if len(r) > 3 else ""
                coupon = _coupon_from(name)
                seg = "T-Bill" if re.search(r"treasury bill|t-bill|tbill", name, re.I) \
                    else _segment_from_text(name)
                add({
                    "isin": (r[1] or "").strip().upper(),
                    "name": name.strip(),
                    "coupon": coupon,
                    "coupon_label": "",
                    "issuer": "Government of India" if seg in ("G-Sec", "T-Bill") else "",
                    "segment": seg,
                    "sectype": "",
                    "rating": "",
                    "face_value": 100.0,
                    "issue_date": None,
                    "maturity_date": _iso_date(maturity) or _maturity_in_name(name),
                    "coupon_freq": "",
                    "last_trade_date": None,
                    "last_price": None,
                    "last_trade_value_lakhs": None,
                    "last_yield": None,
                    "wa_price": None,
                    "wa_yield": None,
                    "traded_value_cr": None,
                    "status": "",
                    "source": "zerodha debt holdings",
                })
        except Exception:
            pass
    return list(out.values())


def _segment_from_text(name: str) -> str:
    s = str(name or "")
    if re.search(r"treasury bill|t-?bill|t\w*\s?\d{3}\s?d", s, re.I):
        return "T-Bill"
    if re.search(r"\bGOI\b|G-SEC|GSEC|GILT", s, re.I):
        return "G-Sec"
    if "SDL" in s.upper() or re.search(r"(?:^|\s)(?:UP|MH|KA|TN|WB|GJ|HR|RJ|MP|PB|OD|KL|TP|TS)\b", s):
        return "State Govt / SDL"
    if re.search(r"(\d+(?:\.\d+)?%[A-Za-z ]*)", s):
        if re.search(r"strips|strips|STRPP", s, re.I):
            return "G-Sec STRIPS" if "GOI" in s.upper() else "STRIPPED Debt"
        return "Corporate Bond"
    return "Corporate Bond"


# --------------------------------------------------------------------------
# Catalog build
# --------------------------------------------------------------------------

_SORT_KEY = {"T-Bill": 0, "G-Sec": 1, "G-Sec FRB": 1, "G-Sec STRIPS": 1,
             "State Govt / SDL": 2, "PSU / Sub-sovereign": 3,
             "Corporate Bond": 4, "STRIPPED Debt": 5,
             "Certificate of Deposit": 6, "Commercial Paper": 7}


def _latest_raw_set() -> tuple[date, dict[str, Path]] | None:
    """Newest raw date that has at least a corp master or wdm list."""
    if not RAW_DIR.is_dir():
        return None
    dates = []
    for d in RAW_DIR.iterdir():
        if not d.is_dir() or not d.name[:4].isdigit():
            continue
        names = {p.name for p in d.glob("*.csv") if p.stat().st_size > 0}
        if any(n.startswith(("corp_master_", "wdm_list_", "cbm_trades_"))
               for n in names):
            try:
                dates.append((datetime.strptime(d.name, "%Y-%m-%d").date(), d, names))
            except ValueError:
                continue
    if not dates:
        return None
    dates.sort(key=lambda x: x[0], reverse=True)
    return dates[0][0], {n: (dates[0][1] / n) for n in dates[0][2]}


def build_catalog(as_of: date | None = None) -> dict:
    """Merge every raw dump (latest per ISIN) + local seeds into the bond
    universe, computing YTM where the reported yield is missing."""
    rows: dict[str, dict] = {}

    def insert(rec: dict, priority: int = 1) -> None:
        isin = (rec.get("isin") or "").upper()
        if not isin:
            return
        cur = rows.get(isin)
        if cur is None or priority < cur["_prio"]:
            rec = dict(rec)
            rec["_prio"] = priority
            rows[isin] = rec
        elif priority == cur["_prio"]:
            for k, v in rec.items():
                if v and not cur.get(k):
                    cur[k] = v

    raw_set = _latest_raw_set()
    if raw_set is None:
        # Fresh container / no cached dumps yet: build from local seeds +
        # any live-API snapshot so the Bonds tab still works. The next
        # successful fetch_day will populate everything.
        print("  ! bonds: no cached raw dumps; catalog built from seeds only")
        d, files = date.today(), {}
        used_sources: list[str] = ["local seeds (no NSE raw dumps cached)"]
    else:
        d, files = raw_set
        used_sources: list[str] = []
    if files:
        cur_date = d
        for name in sorted(files):
            path = files[name]
            if name.startswith("corp_master_"):
                try:
                    for rec in parse_corp_master(path):
                        insert(rec, 1)
                    used_sources.append(f"CBM security master {cur_date}")
                except Exception as e:  # noqa: BLE001
                    print(f"  ! corp master parse: {e}")
            if name.startswith("wdm_list_"):
                try:
                    for rec in parse_wdm(path):
                        insert(rec, 2)
                    used_sources.append(f"WDM securities list {cur_date}")
                except Exception as e:  # noqa: BLE001
                    print(f"  ! wdm parse: {e}")
            if name.startswith("cbm_trades_"):
                try:
                    for tr in parse_cbm_trades(path):
                        bond = rows.get(tr["isin"])
                        if bond is None:
                            r = {**tr, "name": "", "coupon": None,
                                 "coupon_freq": "", "issuer": "",
                                 "segment": "Corporate Bond",
                                 "face_value": 100.0,
                                 "status": "", "source": "CBM daily trades"}
                            # traded-away-from-master rows carry a REPORTED yield
                            # (and price) — resolve it so they get a YTM too.
                            _finalize_rec(r)
                            insert(r, 3)
                        else:
                            if tr.get("last_price") and not bond.get("last_price"):
                                bond["last_price"] = tr["last_price"]
                            if tr.get("last_yield") and not bond.get("last_yield"):
                                bond["last_yield"] = tr["last_yield"]
                            if tr.get("wa_yield") and not bond.get("wa_yield"):
                                bond["wa_yield"] = tr["wa_yield"]
                            if tr.get("last_trade_date"):
                                if not bond.get("last_trade_date") or \
                                        tr["last_trade_date"] > bond["last_trade_date"]:
                                    bond["last_trade_date"] = tr["last_trade_date"]
                            _finalize_rec(bond)
                    used_sources.append(f"CBM daily trades {cur_date}")
                except Exception as e:  # noqa: BLE001
                    print(f"  ! cbm trades parse: {e}")

    # local seeds fill the gaps (G-Secs/SDLs never traded during the window)
    for rec in _seed_records():
        insert(rec, 5)

    # live snapshot row merge (records absent from bulk files)
    if LIVE_JSON.exists():
        try:
            doc = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
            for rec in doc.get("records") or []:
                r = {
                    "isin": str(rec.get("isin") or "").strip().upper(),
                    "name": str(rec.get("name") or "").strip(),
                    "coupon": _num(rec.get("coupon")),
                    "coupon_freq": "",
                    "issuer": str(rec.get("issuer") or "").strip(),
                    "segment": _segment_from_text(str(rec.get("name") or "")),
                    "face_value": 100.0,
                    "maturity_date": _iso_date(rec.get("maturity_date")),
                    "last_price": _num(rec.get("price")),
                    "last_yield": _num(rec.get("ytm")),
                    "status": "",
                    "source": "live debt-market API",
                }
                _finalize_rec(r)
                insert(r, 4)
            used_sources.append(f"live debt-market API ({doc.get('fetched_at')})")
        except Exception:  # noqa: BLE001
            pass

    bonds = []
    for isin, rec in rows.items():
        rec.pop("_prio", None)
        if not rec.get("name"):
            rec["name"] = rec.get("issuer") or isin
        bonds.append(rec)

    bonds.sort(key=lambda b: (_SORT_KEY.get(b.get("segment"), 9),
                              b.get("ytm") is None,
                              -(b.get("ytm") or 0),
                              b.get("name") or ""))

    catalog = {
        "as_of": (d or date.today()).isoformat(),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "sources": sorted(set(used_sources)),
        "n_bonds": len(bonds),
        "segments": {},
        "bonds": bonds,
    }
    for b in bonds:
        seg = b.get("segment") or "Other"
        catalog["segments"][seg] = catalog["segments"].get(seg, 0) + 1
    CATALOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_JSON.write_text(json.dumps(catalog, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return catalog


def corsed(name: str, prefix: str) -> bool:
    return name.startswith(prefix)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="NSE bond market data fetch/catalog")
    parser.add_argument("--days", type=int, default=1,
                        help="fetch the last N calendar days (trading days only; default 1)")
    parser.add_argument("--build-only", action="store_true",
                        help="rebuild the catalog from previously cached raw files")
    parser.add_argument("--live", action="store_true",
                        help="also attempt the live debt-market JSON API")
    args = parser.parse_args(argv)

    if not args.build_only:
        # Walk back day-by-day from today; weekend/holiday dates 404 and are
        # skipped. ``args.days`` counts successful fetch days.
        got, tries, d = 0, 0, date.today()
        while got < args.days and tries < args.days * 3 + 5:
            files = fetch_day(d)
            if files:
                print(f"  {d}: {len(files)} file(s)")
                got += 1
            d -= timedelta(days=1)
            tries += 1
            time.sleep(0.2)

    if args.live:
        recs = fetch_live_debt_market()
        print(f"  live API: {len(recs)} record(s)")

    catalog = build_catalog()
    segs = dict(sorted(catalog["segments"].items(), key=lambda kv: kv[1], reverse=True))
    print(f"\nBond catalog -> {CATALOG_JSON}")
    print(f"  as_of     : {catalog['as_of']}")
    print(f"  bonds     : {catalog['n_bonds']}")
    print(f"  segments  : {segs}")
    print(f"  sources   : {catalog['sources']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
