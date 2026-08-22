"""Shared helpers for the stock agents (identity / price / actions / reports).

Pulls together the HTTP session logic, date utilities and the on-disk stock data
directories so the per-feature agents stay small.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STOCKS_DIR = BASE_DIR / "data" / "stocks"
IDENTITY_JSON = STOCKS_DIR / "identity.json"
HISTORY_DIR = BASE_DIR / "data" / "stock_history"
ACTIONS_DIR = BASE_DIR / "data" / "stock_actions"
REPORTS_DIR = BASE_DIR / "data" / "stock_reports"
MANUAL_DIR = BASE_DIR / "data" / "raw" / "stock_manual"

EQUITY_ISINS_CSV = BASE_DIR / "data" / "reference" / "equity_isins.csv"
NIFTY_CONSTITUENTS_DIR = BASE_DIR / "data" / "nifty" / "constituents"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_DATE_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def make_opener(cookies: bool = False) -> urllib.request.OpenerDirector:
    handlers = [urllib.request.HTTPSHandler(context=_ssl_ctx())]
    if cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return urllib.request.build_opener(*handlers)


def http_get(url: str, headers: dict | None = None, timeout: int = 30,
             opener: urllib.request.OpenerDirector | None = None,
             retries: int = 3) -> bytes:
    """GET a URL with a browser UA, gzip handling and a couple of retries."""
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    hdrs.update(headers or {})
    op = opener or make_opener()
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with op.open(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, OSError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed {url}: {last}")


def norm_date(s: str) -> str:
    """Normalise a date string to 'DD-Mon-YYYY'. Accepts DD-Mon-YYYY, YYYY-MM-DD, DD/MM/YYYY."""
    s = (s or "").strip()
    m = _DATE_RE.match(s)
    if m:
        return f"{int(m.group(1)):02d}-{m.group(2)[:3]}-{m.group(3)}"
    m = _ISO_RE.match(s)
    if m:
        mon = next((mo for mo, i in _MONTHS.items() if i == m.group(2)), m.group(2))
        return f"{m.group(3)}-{mon}-{m.group(1)}"
    m = _DMY_RE.match(s)
    if m:
        mon = next((mo for mo, i in _MONTHS.items() if i == m.group(2)), m.group(2))
        return f"{int(m.group(1)):02d}-{mon}-{m.group(3)}"
    return s


def date_key(s: str) -> str:
    """'DD-Mon-YYYY' -> 'YYYY-MM-DD' for sorting/comparison."""
    m = _DATE_RE.match((s or "").strip())
    if m:
        return f"{m.group(3)}-{_MONTHS.get(m.group(2)[:3], m.group(2))}-{int(m.group(1)):02d}"
    return s


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")