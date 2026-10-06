"""ICICI Prudential monthly portfolio disclosures, via the site's own API.

Why this exists
---------------
The registry pointed at the downloads SPA
(``/news-and-media/downloads?...MonthlyPortfolioDisclosures``), which is a
JS-rendered shell: scraping its HTML yields only undated evergreen factsheets
(``icici-prudential-balanced-advantage-fund.pdf``), never the dated monthly
disclosures. That is why ICICI holdings silently stalled at one month.

The real data comes from two endpoints, both discovered from the page's own
network traffic:

1. ``POST /nms/v1/downloads/files`` - lists documents. Needs the gateway's
   custom headers (``env: api``, ``sourceurl: DOWNLOADS``, ``requestapiid``);
   without them the gateway answers 405 even for a byte-identical payload.
2. ``https://www.icicipruamc.com/blob<record.url>`` - the archive itself. The
   API returns a path like ``/downloads/Files/...``; it is only served under
   the ``/blob`` prefix, and the download is a ZIP holding one XLSX per scheme.

Politeness: two requests per month with a delay between months, no retry
storming, no WAF evasion.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
RAW = BASE / "data" / "raw" / "pdfs" / "ICICI_Prudential_Mutual_Fund"
API = "https://apimf.icicipruamc.com/nms/v1/downloads/files"
# Monthly Portfolio Disclosures category, captured from the SPA's request.
CATEGORY_ID = "26a073d7-08d2-4a95-95fa-f83a4ee51e40"
BLOB = "https://www.icicipruamc.com/blob"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DELAY = 2.0

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _api_headers() -> dict:
    return {"User-Agent": UA, "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.icicipruamc.com/",
            "Origin": "https://www.icicipruamc.com",
            "env": "api", "sourceurl": "DOWNLOADS",
            "requestapiid": str(uuid.uuid4())}


def _blob_headers() -> dict:
    return {"User-Agent": UA, "Accept": "*/*",
            "Referer": "https://www.icicipruamc.com/",
            "env": "api", "sourceurl": "DOWNLOADS",
            "requestapiid": str(uuid.uuid4())}


def list_documents(size: int = 100, pages: int = 4) -> list[dict]:
    """Every Monthly Portfolio Disclosure the API will return, newest first."""
    out: list[dict] = []
    for p in range(1, pages + 1):
        payload = {"categoryId": CATEGORY_ID, "schemeCategory": "",
                   "userType": "Investor", "fileType": "All", "page": str(p),
                   "size": str(size), "filter": [], "categoryName": "OTHERS"}
        req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                     headers=_api_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode())
        files = ((body.get("success") or {}).get("data") or {}).get("files") or []
        if not files:
            break
        out.extend(f for f in files
                   if "monthly" in (f.get("categoryName") or "").lower())
        time.sleep(DELAY)
        if len(files) < size:
            break
    return out


def _month_key(rec: dict) -> str | None:
    """YYYY-MM from the record's own title; API timestamps are unreliable."""
    title = (rec.get("title") or {}).get("text", "")
    m = re.search(r"(%s)\s+(\d{4})" % "|".join(MONTHS), title)
    if not m:
        return None
    return f"{m.group(2)}-{MONTHS.index(m.group(1)) + 1:02d}"


def index_by_month(recs: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for r in recs:
        key = _month_key(r)
        if key and key not in idx:
            idx[key] = r
    return idx


def fetch_archive(rec: dict, retries: int = 3) -> bytes:
    """Download the month's ZIP. ``www`` is a CNAME to Azure Front Door and
    occasionally fails to resolve, so a DNS hiccup is worth one more try."""
    url = BLOB + urllib.parse.quote(rec["url"])
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_blob_headers())
            with urllib.request.urlopen(req, timeout=180) as r:
                blob = r.read()
            if blob[:2] != b"PK":
                raise ValueError(f"not a zip (got {len(blob)} bytes)")
            return blob
        except Exception as exc:              # noqa: BLE001 - reported, retried
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"archive download failed: {type(last).__name__}: {last}")


def extract_month(blob: bytes, month: str) -> int:
    """Unpack the archive into the canonical raw path for ``month``."""
    y, m = month.split("-")
    dest = RAW / y / m
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name.lower().endswith((".xlsx", ".xls", ".csv")):
                continue
            target = dest / name
            if target.exists() and target.stat().st_size == info.file_size:
                continue
            with z.open(info) as fh:
                target.write_bytes(fh.read())
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", default="",
                    help="comma-separated YYYY-MM (default: newest 2 available)")
    ap.add_argument("--list", action="store_true", help="only list what exists")
    ap.add_argument("--save-zip", action="store_true", help="keep the archive too")
    args = ap.parse_args()

    recs = list_documents()
    idx = index_by_month(recs)
    if not idx:
        print("no monthly portfolio disclosures returned")
        return 1
    months = sorted(idx)
    print(f"available: {len(months)} months "
          f"({months[0]} .. {months[-1]}), newest = {months[-1]}")

    if args.list:
        for m in months[-14:]:
            print(f"  {m}  {idx[m]['title']['text']}")
        return 0

    want = [m.strip() for m in args.months.split(",") if m.strip()]
    if not want:
        want = months[-2:]
    missing = [m for m in want if m not in idx]
    if missing:
        print(f"not published yet: {missing}")

    rc = 0
    for month in [m for m in want if m in idx]:
        rec = idx[month]
        print(f"[{month}] {rec['title']['text']}")
        try:
            blob = fetch_archive(rec)
        except RuntimeError as exc:
            print(f"   FAILED: {exc}")
            rc = 1
            continue
        print(f"   archive {len(blob):,}b")
        if args.save_zip:
            z = RAW / "_api_zips"
            z.mkdir(parents=True, exist_ok=True)
            (z / rec["url"].rsplit("/", 1)[-1]).write_bytes(blob)
        n = extract_month(blob, month)
        print(f"   extracted {n} new file(s) -> data/raw/pdfs/"
              f"ICICI_Prudential_Mutual_Fund/{month.replace('-', '/')}")
        time.sleep(DELAY)
    return rc


if __name__ == "__main__":
    sys.exit(main())