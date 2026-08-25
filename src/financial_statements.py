"""Financial-statements pipeline [stmt-v1.0.0].

Converts NSE financial-results PDFs (the "audit report" announcements whose
URLs stock_reports.py already collects) into normalised quarterly / annual
statements per stock:

    fetch announcements -> download PDF -> extract line items
    (deterministic word-position parser; AI-vision tier for image-only scans
    when ai_extract is configured) -> canonical schema (statement_schema)
    -> derive discrete quarters from cumulative columns -> TTM roll-up
    -> accounting-identity validation -> data/stock_financials/<ISIN>.json

House rules:
- Honest nulls everywhere: an unparsed page is needs_review, never zeros;
  value/quality factors simply stay unavailable until coverage lands.
- Values are Rs crore (unit scale detected per page); per-share items stay
  in rupees.
- Every stored record carries its source URL + sha256 so figures are always
  traceable back to the filed document.

CLI::

    python -m src.financial_statements --symbols RELIANCE --limit 4
    python -m src.financial_statements            # top-held stocks first
    python -m src.financial_statements --download-fr-xbrl [--years 5] [--symbols X,Y]
        # raw download of NSE financial-results XBRL filings (download-first /
        # fill-last: metadata + XML land in data/raw/financial_results_xbrl/,
        # no parsing, no AI extraction)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pdfplumber

from .stock_common import (BASE_DIR, http_get, load_json, nse_session,
                           now_iso, save_json)
from .stock_identity import load_identity
from . import statement_schema as ss

FINANCIALS_DIR = BASE_DIR / "data" / "stock_financials"
RAW_RESULTS_DIR = BASE_DIR / "data" / "raw" / "financial_results"
# Financial-results XBRL filings (the page
# /companies-listing/corporate-filings-financial-results): one XML per filed
# result; audited/unaudited + consolidated/standalone flags ride the metadata.
XBRL_RAW_DIR = BASE_DIR / "data" / "raw" / "financial_results_xbrl"
XBRL_META_PATH = XBRL_RAW_DIR / "_metadata.json"
XBRL_STATUS_PATH = XBRL_RAW_DIR / "_status.json"

NSE_FR_URL = ("https://www.nseindia.com/api/corporates-financial-results"
              "?index=equities&period=Quarterly"
              "&from_date={frm}&to_date={to}")
FR_REFERER = ("https://www.nseindia.com/companies-listing/"
              "corporate-filings-financial-results")

NSE_ANNOUNCE_URL = ("https://www.nseindia.com/api/corporate-announcements"
                    "?index=equities&symbol={sym}&page={page}")

_RESULT_HEADLINE = re.compile(
    r"(financial results|financial result|quarterly result|unaudited "
    r".*results|audited .*results)", re.I)
_EXCLUDE_HEADLINE = re.compile(
    r"(transcript|audio recording|media release|investor|newspaper "
    r"publication|analyst|presentation|earnings call|scrutiniser|voting)",
    re.I)


# ---- financial-results XBRL bulk download --------------------------------------

def _fr_month_windows(start: date, end: date) -> list[tuple[str, str]]:
    out = []
    cur = start
    while cur < end:
        nxt_m = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((cur.strftime("%d-%m-%Y"),
                    min(nxt_m - timedelta(days=1), end).strftime("%d-%m-%Y")))
        cur = nxt_m
    return out


def _fr_fetch_window(frm: str, to: str) -> list[dict]:
    try:
        raw = http_get(NSE_FR_URL.format(frm=frm, to=to),
                       headers={"Referer": FR_REFERER,
                                "Accept": "application/json, text/plain, */*"},
                       timeout=60, opener=nse_session(), retries=2)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _xbrl_download_one(row: dict) -> tuple[str, bool]:
    seq = row.get("seqNumber") or ""
    symbol = (row.get("symbol") or "_unknown").upper()
    url = row.get("xbrl") or ""
    dest = XBRL_RAW_DIR / symbol / f"{symbol}_{seq}.xml"
    if not url:
        return seq, False
    if dest.exists() and dest.stat().st_size > 256:
        return seq, True
    try:
        raw = http_get(url, opener=nse_session(), timeout=60, retries=2)
    except Exception:
        return seq, False
    if raw[:5] != b"<?xml" and b"<xbrl" not in raw[:600]:
        return seq, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return seq, True


def download_financial_results_xbrl(symbols: list[str] | None = None,
                                    years_back: int = 5,
                                    workers: int = 6,
                                    window_pacing_s: float = 0.5) -> dict:
    """Raw download of NSE financial-results filings (metadata + XBRL XMLs).

    Sweeps monthly filing-date windows for the last ``years_back`` years over
    ALL equities (no F&O filter), keeps rows for the requested symbols and
    saves each filing's XBRL XML under
    ``data/raw/financial_results_xbrl/<SYMBOL>/``. Metadata merges by
    ``seqNumber`` into ``_metadata.json`` so reruns only fetch missing files;
    nothing here parses figures or calls the AI extractor."""
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
    else:
        wanted = {(v.get("symbol") or "").upper()
                  for v in ident.values() if v.get("symbol")}
    wanted.discard("")
    end = date.today()
    start = date(end.year - years_back, end.month, 1)

    meta = load_json(XBRL_META_PATH, {}) or {}
    windows = _fr_month_windows(start, end)
    empty_windows = 0
    for i, (frm, to) in enumerate(windows, 1):
        rows = _fr_fetch_window(frm, to)
        if not rows:
            empty_windows += 1
        for r in rows:
            seq = str(r.get("seqNumber") or "")
            if seq and (r.get("symbol") or "").upper() in wanted:
                meta[seq] = r
        if i % 12 == 0:
            print(f"  [meta {i}/{len(windows)}] merged={len(meta)}", flush=True)
        time.sleep(window_pacing_s)
    save_json(XBRL_META_PATH, meta)

    def _dest(r: dict) -> Path:
        sym = (r.get("symbol") or "_unknown").upper()
        return XBRL_RAW_DIR / sym / f"{sym}_{r.get('seqNumber')}.xml"

    todo = [r for r in meta.values() if r.get("xbrl") and not _dest(r).exists()]
    no_link = sum(1 for r in meta.values() if not r.get("xbrl"))
    fetched = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_xbrl_download_one, r): r for r in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            seq, ok = fut.result()
            fetched += ok
            failed += not ok
            if n % 250 == 0:
                print(f"  [dl {n}/{len(todo)}]", flush=True)
            time.sleep(0.15)

    status = {"as_of": now_iso(), "symbols": len(wanted),
              "filings_kept": len(meta), "downloads_attempted": len(todo),
              "downloaded_total": fetched, "failed": failed,
              "no_xbrl_link": no_link, "empty_windows": empty_windows}
    save_json(XBRL_STATUS_PATH, status)
    return status


# ---- announcements -------------------------------------------------------------

def _is_result_announcement(text: str) -> bool:
    t = text or ""
    return bool(_RESULT_HEADLINE.search(t)) \
        and not _EXCLUDE_HEADLINE.search(t)


def fetch_result_announcements(symbol: str, pages: int = 3) -> list[dict]:
    """Deeper-than-stock_reports pull of financial-result announcements."""
    seen: set[str] = set()
    out: list[dict] = []
    for page_no in range(max(1, pages)):
        try:
            raw = http_get(NSE_ANNOUNCE_URL.format(sym=symbol, page=page_no),
                           headers={"Referer": "https://www.nseindia.com/"},
                           timeout=20, opener=nse_session(), retries=1)
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(data, list):
            break
        fresh = False
        for a in data:
            url = a.get("attchmntFile") or ""
            if not url or url in seen:
                continue
            text = f"{a.get('attchmntText') or ''} {a.get('an_subject') or ''}"
            if not _is_result_announcement(text):
                continue
            seen.add(url)
            fresh = True
            out.append({
                "date": (a.get("an_dt") or "")[:11],
                "headline": text.strip()[:260],
                "url": url,
            })
        if not fresh:
            break
        if page_no + 1 < max(1, pages):
            time.sleep(0.4)
    return out


def download_pdf(url: str, symbol: str) -> Path | None:
    dest_dir = RAW_RESULTS_DIR / (symbol.upper() or "_unknown")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1])[-90:]
    dest = dest_dir / (name or "result.pdf")
    if dest.exists() and dest.stat().st_size > 1024:
        return dest
    try:
        raw = http_get(url, opener=nse_session(), timeout=60, retries=2)
    except Exception:
        return None
    if raw[:5] != b"%PDF-":
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


# ---- deterministic extraction ---------------------------------------------------

_NUM_TOKEN = re.compile(r"^\(?\d[\d,]*\.?\d*\)?%?$")


class _Col:
    __slots__ = ("x", "kind", "date")

    def __init__(self, x: float, kind: str, date):
        self.x, self.kind, self.date = x, kind, date


def _lines_from_page(page) -> list[list[dict]]:
    words = page.extract_words(x_tolerance=1.8, y_tolerance=2.5)
    buckets: dict[int, list[dict]] = {}
    for w in words:
        buckets.setdefault(round(w["top"] / 4.0), []).append(w)
    return [sorted(b, key=lambda w: w["x0"])
            for _, b in sorted(buckets.items())]


def _header_columns(lines: list[list[dict]]) -> tuple[list[_Col], int]:
    """Locate the period header (a band of up to 3 lines; scanned PDFs wrap
    dates across lines) extended downwards while fresh dates keep appearing.
    Columns deduped by x-proximity, kept left->right.
    Returns (columns, first_data_line_idx_after_band)."""
    def parse_line(ln):
        out = []
        for w in ln:
            d = ss.parse_period_date(w["text"])
            if d and w["x0"] > 120:
                out.append((w, d))
        return out

    for idx in range(min(25, len(lines))):
        if len(parse_line(lines[idx])) + \
                sum(len(parse_line(ln)) for ln in lines[idx + 1:idx + 3]) < 2:
            continue
        ctx_lines = lines[max(0, idx - 3):idx + 5]
        ctx = " ".join(" ".join(w["text"] for w in ln) for ln in ctx_lines)
        flat = []
        for ln in lines[idx:idx + 3]:
            flat.extend(parse_line(ln))
        cols: list[_Col] = []
        used_x: list[float] = []

        def add(wd):
            w, d = wd
            cx = (w["x0"] + w["x1"]) / 2.0
            if any(abs(cx - ux) < 40 for ux in used_x):
                return False
            used_x.append(cx)
            cols.append(_Col(cx, ss.classify_period(ctx), d))
            return True

        for wd in sorted(flat, key=lambda wd_: wd_[0]["x0"]):
            add(wd)
        last = idx + 3
        while last < min(idx + 6, len(lines)):
            fresh = [wd for wd in parse_line(lines[last])
                     if all(abs((wd[0]["x0"] + wd[0]["x1"]) / 2 - ux) >= 40
                            for ux in used_x)]
            if not fresh:
                break
            for wd in sorted(fresh, key=lambda wd_: wd_[0]["x0"]):
                add(wd)
            last += 1
        if len(cols) >= 2:
            return cols, last
    return ([], -1)


def _align_columns(num_xs: list[float], anchors: list[float],
                   max_dist: float = 75.0) -> dict[int, int]:
    """Monotonic (order-preserving) min-distance alignment of x-sorted figure
    columns onto x-sorted header anchors -> {num_idx: anchor_idx}.

    Tolerates OCR x-drift globally instead of greedily per cell and allows
    skipped columns on either side (empty cells / unused comparatives)."""
    n, m = len(num_xs), len(anchors)
    INF = float("inf")
    PENALTY = max_dist          # leaving an item unpaired costs as much as
                                # the worst acceptable pairing, so any pair
                                # closer than max_dist strictly wins
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    take: list[list[tuple | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dist = abs(num_xs[i - 1] - anchors[j - 1])
            options = []
            if dist <= max_dist:
                options.append((dp[i - 1][j - 1] + dist, "pair"))
            options.append((dp[i - 1][j] + PENALTY, "skip_num"))
            options.append((dp[i][j - 1] + PENALTY, "skip_anchor"))
            best_cost, best_choice = min(options, key=lambda o: o[0])
            dp[i][j] = best_cost
            take[i][j] = best_choice
    out: dict[int, int] = {}
    i, j = n, m
    while i > 0 and j > 0:
        ch = take[i][j]
        if ch is None:
            break
        if ch == "pair":
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif ch == "skip_num":
            i -= 1
        else:
            j -= 1
    return out


def _section_of(text_upper: str) -> str:
    return "consolidated" if "CONSOLIDAT" in text_upper else "standalone"


def _canonical_slots(primary: tuple[str, tuple]) -> list[tuple[str, tuple]]:
    """Reg-33 comparative sequence implied by the filing's headline period,
    ordered as printed (left->right)."""
    kind, (y, m, d) = primary
    yago = (y - 1, m, d)
    fy_end = (y + 1 if m <= 3 else y, 3, 31)
    fy_prev = (y - 1 if m <= 3 else y, 3, 31)
    if kind == "FY" or m == 3:
        return [("FY", (y, 3, 31)), ("FY", fy_prev)]
    if m == 6:
        return [("Q", (y, m, d)), ("Q", yago), ("FY", fy_end)]
    if m == 9:
        return [("Q", (y, m, d)), ("H1", (y, 9, d)), ("Q", yago),
                ("H1", (y - 1, 9, d)), ("FY", fy_end)]
    if m == 12:
        return [("Q", (y, m, d)), ("9M", (y, 12, d)), ("Q", yago),
                ("9M", (y - 1, 12, d)), ("FY", fy_end)]
    return [("Q", (y, m, d)), ("Q", yago)]


def _figure_clusters(lines: list[list[dict]], gap: float = 24.0
                     ) -> list[float]:
    """Data-driven figure-column centers: cluster every numeric token's
    x-center (within-table consistency survives scan drift)."""
    xs: list[float] = []
    for line in lines:
        joined = " ".join(w["text"] for w in line)
        if _TABLE_END.search(joined):
            break
        for w in line:
            t = w["text"]
            if _NUM_TOKEN.match(t) and any(ch.isdigit() for ch in t):
                v = ss.to_number(t)
                if v is None:
                    continue
                xs.append((w["x0"] + w["x1"]) / 2.0)
    if not xs:
        return []
    xs.sort()
    clusters: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    big = [c for c in clusters if len(c) >= 3]
    if not big:
        return sorted(sum(c) / len(c) for c in clusters)
    return sorted(sum(c) / len(c) for c in big)


def _cluster_hit_counts(lines: list[list[dict]],
                        centers: list[float]) -> list[int]:
    hits = [0] * len(centers)
    for line in lines:
        for w in line:
            t = w["text"]
            if not (_NUM_TOKEN.match(t) and any(ch.isdigit() for ch in t)):
                continue
            cx = (w["x0"] + w["x1"]) / 2.0
            nearest_i = min(range(len(centers)),
                            key=lambda i: abs(centers[i] - cx))
            if abs(centers[nearest_i] - cx) <= 24.0:
                hits[nearest_i] += 1
    return hits


def _row_values(line: list[dict], anchors: list[float]
                ) -> tuple[str, list[tuple[int, float]]]:
    """Split one visual line into label + [(anchor_index, number)].
    Numbers stay UNSCALED here; the caller applies unit scale after the
    label is matched (per-share items must not be scaled)."""
    numeric = []
    label_parts = []
    for w in line:
        t = w["text"]
        if _NUM_TOKEN.match(t) and any(ch.isdigit() for ch in t):
            numeric.append(w)
        else:
            label_parts.append(t)
    if not numeric:
        return "", []
    label = " ".join(label_parts).strip()
    pairs: list[tuple[int, float]] = []
    num_xs = [(w["x0"] + w["x1"]) / 2.0 for w in numeric]
    mapping = _align_columns(num_xs, anchors)
    for num_idx, col_idx in mapping.items():
        v = ss.to_number(numeric[num_idx]["text"])
        if v is None:
            continue
        pairs.append((col_idx, v))
    return label, pairs


_TABLE_END = re.compile(r"notes?|significative|accounting ratios|"
                        r"shareholding pattern", re.I)
_SKIP_LINE = re.compile(r"^(as at|as on|figures?|see |note )", re.I)


def parse_table_pages(pdf_path: Path, max_pages: int = 12,
                      primary_period: tuple[str, tuple] | None = None
                      ) -> list[dict]:
    """Deterministic pass over a results PDF -> raw section records.

    ``primary_period`` = ('Q'|'H1'|'9M'|'FY', (y, m, d)) parsed from the
    announcement headline; anchors the column-slot ordering for scans whose
    geometry is unreliable."""
    out: list[dict] = []
    try:
        pdf = pdfplumber.open(str(pdf_path))
    except Exception:
        return out
    try:
        n_pages = min(len(pdf.pages), max_pages)
        for pno in range(n_pages):
            page = pdf.pages[pno]
            text = page.extract_text() or ""
            if not text or "Particulars" not in text:
                continue
            upper = text.upper()
            section = _section_of(upper)
            scale = ss.unit_scale_to_crore(text)
            lines = _lines_from_page(page)
            cols, header_idx = _header_columns(lines)
            if len(cols) < 2:
                continue
            # figure-column anchors come from the DATA (clustered x-centers),
            # their period keys from the headline-driven Reg-33 sequence;
            # geometry keys the column, semantics names it.
            centers = _figure_clusters(lines[header_idx:])
            if primary_period:
                slots = _canonical_slots(primary_period)
            else:
                slots = [(c.kind, c.date) for c in cols]
            if not centers:
                continue
            # drop junk left of every header anchor (scrip codes, page art)
            floor_x = min(c.x for c in cols) - 45.0
            centers = [c for c in centers if c >= floor_x]
            # keep the K busiest clusters (ratio/extra columns are sparse)
            if len(centers) > len(slots):
                hits = _cluster_hit_counts(lines[header_idx:], centers)
                ranked = sorted(range(len(centers)),
                                key=lambda i: -hits[i])[:len(slots)]
                centers = sorted(centers[i] for i in ranked)
            while len(centers) < len(slots):
                slots = slots[:-1] or slots
            started = False
            for line in lines[header_idx:]:
                joined = " ".join(w["text"] for w in line)
                if not started:
                    if re.search(r"income|revenue|particulars", joined,
                                 re.I):
                        started = True
                    continue
                if _TABLE_END.search(joined):
                    break
                if _SKIP_LINE.match(joined):
                    continue
                label, pairs = _row_values(line, centers[:len(slots)])
                if not label or not pairs:
                    # continuation of a wrapped label: nothing to do here
                    continue
                canon, score = ss.match_label(label)
                values = {}
                scale_eff = 1.0 if canon in ss.PER_SHARE_KEYS else scale
                for col_idx, amount in pairs:
                    kind, date = slots[col_idx]
                    values[f"{kind}|{date[0]}-{date[1]}-{date[2]}"] = \
                        round(amount * scale_eff, 4)
                out.append({
                    "section": section,
                    "label_raw": label[:160],
                    "canon": canon,
                    "match_score": round(score, 3),
                    "exact": bool(ss.is_exact_label(label)),
                    "face_value": _extract_face_value(label),
                    "values": values,
                    "page": pno,
                })
    finally:
        pdf.close()
    return out


# ---- AI vision tier (PRIMARY extraction path) -----------------------------------

AI_CACHE_DIR = RAW_RESULTS_DIR / "_ai_cache"
PROMPT_VERSION = "p3"       # bump to invalidate cached extractions

_STMT_SYSTEM = (
    "You are a precise financial-data extraction engine for Indian listed-"
    "company results filings (SEBI Reg-33 format). From the provided filing "
    "page, extract EVERY line-item row of the main financial-results table "
    "EXACTLY as printed. Rules:\n"
    "- Output ONLY a JSON object matching the requested schema.\n"
    "- 'section' is 'consolidated' or 'standalone' per the page title.\n"
    "- 'unit' is one of 'crore', 'lakh', 'million' per the page's amount "
    "note (e.g. '(Rs. in crore)').\n"
    "- List every period COLUMN of the table under 'periods' in printed "
    "left-to-right order, each as {kind: Q|H1|9M|FY, date: YYYY-MM-DD}.\n"
    "- Each row's 'values' array MUST have exactly one entry per period, in "
    "the same order; use null for an empty cell.\n"
    "- Numbers only (no commas, no currency); bracketed figures are negative; "
    "per-share rows (EPS, face value) stay unscaled rupees.\n"
    "- Keep labels verbatim. Do NOT skip the per-share rows: 'Basic EPS', "
    "'Diluted EPS' and 'Face Value' lines MUST be extracted even when they "
    "sit below the main table or inside a small ratios strip.\n"
    "- DISAMBIGUATE borrowings by their liability group: a 'Borrowings' line "
    "under NON-CURRENT liabilities must be labelled 'Long Term Borrowings'; "
    "under CURRENT liabilities label it 'Short Term Borrowings'.\n"
    "- Ignore audit reports, notes, subsidiary lists, shareholding patterns "
    "and footnotes."
)

_STMT_PROMPT = (
    "Extract the financial-results table from this filing page. Respond with "
    'JSON: {"sections": [{"section": str, "unit": str, '
    '"periods": [{"kind": str, "date": str}], '
    '"rows": [{"label": str, "values": [number|null, ...]}]}]}. '
    "If this page has no results table, respond with {\"sections\": []}."
)


_FACE_VALUE_RE = re.compile(
    r"face value of\s*(?:rs\.?\s*)?(\d+(?:\.\d+)?)", re.I)


def _extract_face_value(label: str) -> float | None:
    m = _FACE_VALUE_RE.search(label or "")
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v if 0.5 <= v <= 1000 else None
    except ValueError:
        return None


def _candidate_pages(pdf_path: Path, max_pages: int = 8) -> list[int]:
    """Pages likely holding statement tables: keyword hits on the text layer;
    image-only pages qualify blindly (scans) — capped by max_pages."""
    import fitz
    keywords = ("particulars", "revenue from operations", "balance sheet",
                "cash flow", "total income", "profit after tax",
                "borrowings", "inventories", "trade payables")
    eps_keywords = ("earnings per share", "basic eps", "diluted eps",
                    "face value")
    picks: list[int] = []
    blind_started = False
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    try:
        for i in range(min(len(doc), 16)):
            txt = doc[i].get_text() or ""
            stripped = txt.strip()
            if len(stripped) < 200:
                if not blind_started:
                    blind_started = True     # first image-only page of a scan
                    picks.append(i)
                continue
            low = stripped.lower()
            score = sum(k in low for k in keywords)
            has_eps = any(k in low for k in eps_keywords)
            if score >= 2 or has_eps:
                picks.append(i)
            if len(picks) >= max_pages:
                break
    finally:
        doc.close()
    return picks[:max_pages]


def _parse_json_loose(content: str):
    """Strip code fences / leading prose; return parsed dict or None."""
    if not content:
        return None
    txt = content.strip()
    txt = re.sub(r"^```(?:json)?", "", txt).strip()
    txt = re.sub(r"```$", "", txt).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _stmt_chat(pdf_path: Path, page_index: int) -> dict | None:
    """One vision call -> parsed JSON payload (or None). One strict-retry
    when the first answer is not valid JSON."""
    import os
    import httpx
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key or os.environ.get("STMT_AI", "").lower() in ("0", "false"):
        return None
    model = os.environ.get("STMT_AI_MODEL", "google/gemini-2.5-flash")
    import fitz
    from PIL import Image
    import base64
    import io
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_index]
        pix = page.get_pixmap(dpi=150)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        b64 = base64.b64encode(buf.getvalue()).decode()
    finally:
        doc.close()

    def call(extra_user: str) -> str | None:
        payload = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": 8000,
            "messages": [
                {"role": "system", "content": _STMT_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": _STMT_PROMPT + extra_user},
                    {"type": "image_url",
                     "image_url": {"url":
                                   f"data:image/jpeg;base64,{b64}"}}]},
            ],
        }
        last_err = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                with httpx.Client(timeout=120) as client:
                    r = client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}",
                                 "Content-Type": "application/json"},
                        json=payload)
                    if r.status_code >= 500 or r.status_code == 429:
                        last_err = RuntimeError(f"HTTP {r.status_code}")
                        continue
                    r.raise_for_status()
                    return (r.json().get("choices") or [{}])[0].get(
                        "message", {}).get("content", "")
            except Exception as e:                   # noqa: BLE001
                last_err = e
        print(f"    AI call failed p{page_index}: {last_err}")
        return None

    content = call("")
    payload = _parse_json_loose(content or "")
    if payload is None and content:
        content2 = call("\nIMPORTANT: your previous reply was not valid "
                        "JSON. Reply again with ONLY the JSON object.")
        payload = _parse_json_loose(content2 or "")
    return payload


def _page_cached_chat(pdf_path: Path, page_index: int, sha: str):
    """_stmt_chat with a per-page JSON cache: successes never re-bill and
    transient failures only delay, never erase, sibling pages."""
    cache_path = AI_CACHE_DIR / f"{sha}-p{page_index}-{PROMPT_VERSION}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload = _stmt_chat(pdf_path, page_index)
    if payload:
        AI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass
    return payload


def ai_extract_document(pdf_path: Path, max_pages: int = 8,
                        use_cache: bool = True) -> list[dict] | None:
    """AI-first extraction of a whole results PDF.

    Returns raw rows in the SAME internal shape as parse_table_pages so the
    normalisation/assembly stages are tier-agnostic:
        [{section, label_raw, canon, match_score, values:{KIND|y-m-d: amt}, page}]
    None means 'AI unavailable' (no key / disabled / all pages failed) —
    callers then fall back to the deterministic parser.
    Results cached by file sha256 so backfills never re-bill.
    """
    import os
    if os.environ.get("OPENROUTER_API_KEY", "") == "" \
            or os.environ.get("STMT_AI", "").lower() in ("0", "false"):
        return None
    sha = sha256_file(pdf_path)
    pages = _candidate_pages(pdf_path, max_pages=max_pages)
    if not pages:
        return None
    out: list[dict] = []
    for pno in pages:
        payload = _page_cached_chat(pdf_path, pno, sha) if use_cache \
            else _stmt_chat(pdf_path, pno)
        if not payload:
            continue
        for sec_block in (payload.get("sections") or []):
            section = str(sec_block.get("section") or "").lower()
            section = "consolidated" if "consol" in section else "standalone"
            unit = str(sec_block.get("unit") or "crore").lower()
            scale = {"crore": 1.0, "lakh": 0.10, "lakhs": 0.10,
                     "million": 10.0}.get(unit, 1.0)
            periods = sec_block.get("periods") or []
            slot_dates: list[tuple[str, tuple] | None] = []
            for p in periods:
                d = ss.parse_period_date(str(p.get("date") or ""))
                kind = ss.classify_period(f"{p.get('kind') or ''} quarter "
                                          f"{p.get('date') or ''}",
                                          default=str(p.get("kind") or "Q"))
                kind = str(p.get("kind") or kind).upper()
                if kind not in ("Q", "H1", "9M", "FY"):
                    kind = "Q"
                slot_dates.append((kind, d) if d else None)
            if not slot_dates or any(s is None for s in slot_dates):
                continue                      # unusable column contract
            for row in (sec_block.get("rows") or []):
                label = str(row.get("label") or "").strip()
                vals = row.get("values")
                if not label or not isinstance(vals, list):
                    continue
                canon, score = ss.match_label(label)
                exact_flag = bool(ss.is_exact_label(label))
                values: dict[str, float] = {}
                scale_eff = 1.0 if canon in ss.PER_SHARE_KEYS else scale
                for j, num in enumerate(vals):
                    if j >= len(slot_dates) or num is None:
                        continue
                    if not isinstance(num, (int, float)):
                        continue
                    kind, date = slot_dates[j]
                    values[f"{kind}|{date[0]}-{date[1]}-{date[2]}"] = \
                        round(float(num) * scale_eff, 4)
                if not values:
                    continue
                out.append({"section": section,
                            "label_raw": label[:160],
                            "canon": canon,
                            "match_score": round(score, 3),
                            "exact": exact_flag,
                            "face_value": _extract_face_value(label),
                            "values": values, "page": pno})
    if not out:
        return None
    matched = sum(1 for r in out if r["canon"])
    if matched < 3:
        return None                             # garbage guard
    return out


# ---- assembly -------------------------------------------------------------------

# expense-family lines are stored positive; filings sometimes repeat them
# as negative outflows in adjacent statements (e.g. "(i) Finance Cost")
_EXPENSE_KEYS = frozenset((
    "finance_costs", "cost_of_materials", "employee_benefits",
    "depreciation_amortisation", "other_expenses", "tax_expense", "capex",
))


def build_section_records(raw_rows: list[dict]) -> dict[str, dict[tuple, dict]]:
    """raw extraction rows -> {section: {(kind,end): {canon: value}}}."""
    sections: dict[str, dict[tuple, dict]] = {}
    for row in raw_rows:
        sec = sections.setdefault(row["section"], {})
        for key_str, amount in row["values"].items():
            kind, date_str = key_str.split("|", 1)
            y, mth, d = (int(x) for x in date_str.split("-"))
            rec_key = (kind, (y, mth, d))
            rec = sec.setdefault(rec_key, {})
            if row["canon"]:
                if row["canon"] in _EXPENSE_KEYS and amount < 0:
                    amount = -amount
                existing = rec.get(row["canon"])
                existing_rank = rec.get("_rank", {}).get(row["canon"], (-1,))
                # rank = (exact-alias hit, fuzzy score): a verbatim
                # 'Revenue from Operations' beats an exact alias hit on the
                # gross-sales line above it.
                rank = (1 if row.get("exact") else 0, row["match_score"])
                if existing is None or rank > existing_rank:
                    rec[row["canon"]] = amount
                    rec.setdefault("_rank", {})[row["canon"]] = rank
            if row.get("face_value"):
                rec["_face_value"] = row["face_value"]
        # derived keys (ebitda / total_debt / net_worth / total_liabilities)
        # are filled once per period after all of its raw rows landed
        for k in list(sec.keys()):
            sec[k] = ss.derive_ebitda(sec[k])
    return sections


def _rec_meta(kind: str, end: tuple[int, int, int]) -> dict:
    fy = ss.fiscal_year((end[0], end[1]), kind)
    meta = {"period_end": f"{end[0]:04d}-{end[1]:02d}-{end[2]:02d}",
            "fy": fy, "kind": kind}
    if kind == "Q":
        q = ss.quarter_of_month(end[1], "Q")
        meta["quarter"] = q
        meta["cumulative"] = False
    else:
        meta["cumulative"] = True
        meta["quarter"] = None
    return meta


def assemble(section_map: dict[tuple, dict]) -> tuple[list[dict], list[dict]]:
    """All raw period records -> (quarters[], annual[]) sorted lists with
    discrete-quarter derivation inside each FY chain."""
    entries: list[tuple[tuple, dict]] = []
    for (kind, end), values in section_map.items():
        rec = dict(values)
        rec.update(_rec_meta(kind, end))
        entries.append(((kind, end), rec))

    # --- discrete-quarter derivation within FY chains ---
    def find(kind_q: str, fy: str, month: int) -> dict | None:
        for (_k, e), rec in entries:
            if _k == kind_q and rec["fy"] == fy and e[1] == month:
                return rec
        return None

    derived: list[tuple[tuple, dict]] = []
    fys = {rec["fy"] for (_k, _e), rec in entries if rec.get("quarter")}
    for fy in sorted(fys):
        q1 = find("Q", fy, 6)
        h1 = find("H1", fy, 9)
        nine = find("9M", fy, 12)
        annual = [rec for (_k, e), rec in entries
                  if _k == "FY" and rec["fy"] == fy and e[1] == 3]
        annual_rec = annual[0] if annual else None
        # A derived discrete quarter subtracts the previous cumulative record
        # from the next one in the same FY chain; the DERIVED quarter's end
        # date IS the later cumulative record's end date (Q2 ends where H1
        # ends, Q3 where 9M ends, Q4 where the audited FY ends).
        chains = []
        if q1 and h1:
            chains.append(("Q2", h1, q1))
        if h1 and nine:
            chains.append(("Q3", nine, h1))
        if nine and annual_rec:
            chains.append(("Q4", annual_rec, nine))
        for tag, cum, prev in chains:
            vals = {}
            for k, v in cum.items():
                if k.startswith("_") or isinstance(v, str):
                    continue
                pv = prev.get(k)
                if isinstance(pv, (int, float)) and isinstance(v, (int, float)):
                    vals[k] = round(v - pv, 4)
            base_end = [e for (k, e), r in entries if r is cum][0]
            meta = _rec_meta("Q", base_end)
            meta["quarter"] = tag
            meta["derived"] = True
            rec = {**vals, **meta}
            derived.append((("QD", base_end), rec))
    all_entries = entries + derived

    quarters, annuals = [], []
    for (_k, end), rec in sorted(all_entries, key=lambda kv: kv[0][1]):
        if rec.get("quarter"):
            quarters.append(rec)
        elif _k == "FY":
            annuals.append(rec)
    # dedupe quarters on period_end keeping non-derived originals first
    seen: dict[str, dict] = {}
    for rec in quarters:
        key = rec["period_end"]
        if key in seen and seen[key].get("derived") and not rec.get("derived"):
            seen[key] = rec
        elif key not in seen:
            seen[key] = rec
    quarters = sorted(seen.values(),
                      key=lambda r: (r["fy"], r["quarter"] or ""))
    years_seen: dict[str, dict] = {}
    for rec in annuals:
        years_seen.setdefault(rec["period_end"], rec)
    annuals = [years_seen[k] for k in sorted(years_seen)]
    return quarters, annuals


def compute_ttm(quarters: list[dict]) -> dict | None:
    """Sum of the last four DISCRETE quarters per line item."""
    disc = sorted((r for r in quarters if not r.get("cumulative")),
                  key=lambda r: r["period_end"])
    if len(disc) < 4:
        return None
    window = disc[-4:]
    # contiguity: consecutive quarter ends ~3 months apart
    def month_idx(period_end: str) -> int:
        y, m, _d = (int(x) for x in period_end.split("-"))
        return y * 12 + m
    idxs = [month_idx(r["period_end"]) for r in window]
    if any(b - a not in (3, 4) for a, b in zip(idxs, idxs[1:])):
        return None
    keys: set[str] = set()
    for r in window:
        keys.update(k for k in r if not k.startswith("_")
                    and k not in ("period_end", "fy", "kind", "quarter",
                                  "cumulative", "derived"))
    skip_per_share = ss.PER_SHARE_KEYS
    ttm: dict = {"window_start": window[0]["period_end"],
                 "window_end": window[-1]["period_end"]}
    for k in sorted(keys):
        if k in skip_per_share:
            # per-share items must NEVER be summed across a window
            ttm[k] = window[-1].get(k)      # latest reading wins
            continue
        vals = [r.get(k) for r in window]
        if any(not isinstance(v, (int, float)) for v in vals):
            continue
        ttm[k] = round(sum(vals), 4)
    return ttm


def validate_and_score(quarters: list[dict], annuals: list[dict]
                       ) -> tuple[list[str], int]:
    issues: list[str] = []
    conf = 100
    sample = (quarters[-1] if quarters else None) or \
             (annuals[-1] if annuals else None)
    if not sample:
        return ["no_records"], 0
    for rec in ([sample] + annuals[-3:]):
        for issue in ss.validate_statement(rec):
            marker = f"{rec.get('period_end')}: {issue}"
            if marker not in issues:
                issues.append(marker)
    rev = sample.get("revenue_from_operations")
    pat = sample.get("pat")
    if not rev:
        conf -= 25
        issues.append("latest_period_missing_revenue")
    if pat is None:
        conf -= 20
        issues.append("latest_period_missing_pat")
    unmatched = sum(1 for r in ([sample]) if r.get("_unmatched"))
    conf -= 10 * unmatched
    conf -= 8 * min(3, len(issues))
    return issues, max(0, min(100, conf))


# ---- orchestration --------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def process_stock(isin: str, ident_row: dict,
                  max_documents: int = 6) -> dict:
    """Full pipeline for one stock; returns a status summary."""
    symbol = ident_row.get("symbol") or ""
    name = ident_row.get("name") or ""
    summary = {"isin": isin, "symbol": symbol, "documents": 0,
               "status": "skipped"}
    if not symbol:
        return summary
    anns = fetch_result_announcements(symbol, pages=2)
    if not anns:
        summary["status"] = "no_announcements"
        return summary
    anns = anns[:max_documents]
    all_raw: list[dict] = []
    sources: list[dict] = []
    for a in anns:
        primary = ss.primary_period_from_headline(a["headline"])
        pdf_path = download_pdf(a["url"], symbol)
        if not pdf_path:
            continue
        # PRIMARY: vision-model extraction (handles scans + OCR noise);
        # FALLBACK: deterministic word-position parser (offline / no key).
        raw = ai_extract_document(pdf_path)
        tier = "ai"
        if raw is None:
            raw = parse_table_pages(pdf_path, primary_period=primary)
            tier = "deterministic"
        if raw:
            all_raw.extend(raw)
            sources.append({"url": a["url"], "date": a["date"],
                            "sha256": sha256_file(pdf_path), "tier": tier})
    summary["documents"] = len(sources)
    if not all_raw:
        summary["status"] = "extraction_failed"
        return summary

    sections = build_section_records(all_raw)
    doc: dict = {
        "schema_version": ss.SCHEMA_VERSION,
        "isin": isin, "symbol": symbol, "name": name,
        "fetched_at": now_iso(),
        "sources": sources,
    }
    total_issues: list[str] = []
    section_confs: list[int] = []
    for section_name, section_key in (("consolidated", "consolidated"),
                                      ("standalone", "standalone"),
                                      ("auto", "consolidated")):
        smap = sections.get(section_name)
        if not smap:
            continue
        quarters, annuals = assemble(smap)
        if not quarters and not annuals:
            continue
        issues, conf = validate_and_score(quarters, annuals)
        total_issues.extend(f"{section_key}:{i}" for i in issues)
        section_confs.append(conf)
        ttm = compute_ttm(quarters)
        block = {"quarters": quarters, "annual": annuals, "ttm": ttm}
        if section_key in doc and doc[section_key]:
            # merge auto-section into consolidated rather than overwrite
            doc[section_key]["quarters"] = _merge_lists(
                doc[section_key]["quarters"], quarters)
            doc[section_key]["annual"] = _merge_lists(
                doc[section_key]["annual"], annuals)
        else:
            doc[section_key] = block
    if "standalone" not in doc and "consolidated" not in doc:
        summary["status"] = "no_statements"
        return summary
    primary = doc.get("consolidated") or doc.get("standalone")
    issues, conf = validate_and_score(primary.get("quarters"),
                                      primary.get("annual"))
    # a sparse secondary section must not drag the primary's score down
    overall = max([conf] + section_confs) if section_confs else conf
    doc["validation"] = {"issues": total_issues,
                         "confidence": max(0, min(100, overall)),
                         "checked_at": now_iso()}
    save_json(FINANCIALS_DIR / f"{isin}.json", doc)
    nq = len((doc.get("consolidated") or {}).get("quarters") or
             (doc.get("standalone") or {}).get("quarters") or [])
    summary.update({"status": "ok", "quarters": nq,
                    "confidence": doc["validation"]["confidence"]})
    return summary


def _merge_lists(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_key = {r.get("period_end"): r for r in existing}
    for r in incoming:
        cur = by_key.get(r.get("period_end"))
        if cur is None or (cur.get("derived") and not r.get("derived")):
            by_key[r["period_end"]] = r
    return sorted(by_key.values(), key=lambda r: r["period_end"])


STALE_DAYS = 35          # results arrive quarterly; 35d keeps us fresh


def refresh_stale(limit: int = 12) -> list[dict]:
    """Scheduler entry: process the most-held stocks whose statement file is
    missing or older than STALE_DAYS. Cheap when everything is fresh."""
    import time as _t
    from .stock_identity import load_identity
    ident = load_identity()
    candidates: list[str] = []
    try:
        import sqlite3
        con = sqlite3.connect(BASE_DIR / "data" / "webapp.db")
        rows = con.execute(
            "SELECT isin, COUNT(*) AS n FROM holdings WHERE isin IN "
            "(SELECT isin FROM securities WHERE confirmed_equity=1) "
            "GROUP BY isin ORDER BY n DESC").fetchall()
        con.close()
        candidates = [r[0] for r in rows if r[0] in ident]
    except Exception:
        candidates = list(ident.keys())
    now = _t.time()
    target: list[str] = []
    for isin in candidates:
        path = FINANCIALS_DIR / f"{isin}.json"
        if not path.exists() or \
                now - path.stat().st_mtime > STALE_DAYS * 86400:
            target.append(isin)
        if len(target) >= limit:
            break
    out = []
    for isin in target:
        res = process_stock(isin, ident[isin])
        out.append(res)
        _t.sleep(0.3)
    return out


def run(symbols: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """Backfill driver. Default order: stocks most-held by funds first
    (holdings-table frequency as the coverage-priority proxy)."""
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items()
                  if (v.get("symbol") or "") in wanted}
        if limit:
            target = dict(list(target.items())[:limit])
    else:
        priority: list[str] = []
        try:
            import sqlite3
            con = sqlite3.connect(BASE_DIR / "data" / "webapp.db")
            rows = con.execute(
                "SELECT isin, COUNT(*) AS n FROM holdings WHERE isin IN "
                "(SELECT isin FROM securities WHERE confirmed_equity=1) "
                "GROUP BY isin ORDER BY n DESC").fetchall()
            con.close()
            priority = [r[0] for r in rows if r[0] in ident]
        except Exception:
            pass
        rest = [i for i in ident if i not in priority]
        target_ids = (priority + rest)[:limit] if limit else priority + rest
        target = {i: ident[i] for i in target_ids}
    out = []
    for n, (isin, row) in enumerate(target.items(), 1):
        res = process_stock(isin, row)
        out.append(res)
        if n % 5 == 0 or res.get("status") != "ok":
            print(f"  [{n}/{len(target)}] {res.get('symbol')} "
                  f"{res.get('status')}", flush=True)
        time.sleep(0.3)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="",
                        help="comma-separated NSE symbols")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--download-fr-xbrl", action="store_true",
                        help="raw download: financial-results XBRL filings "
                             "-> data/raw/financial_results_xbrl/")
    parser.add_argument("--years", type=int, default=5,
                        help="years back for --download-fr-xbrl")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    if args.download_fr_xbrl:
        result = download_financial_results_xbrl(
            symbols=syms, years_back=args.years, workers=args.workers)
        print(json.dumps(result, indent=2))
        return 0
    results = run(symbols=syms, limit=args.limit)
    ok = sum(1 for r in results if r.get("status") == "ok")
    print(json.dumps({"total": len(results), "ok": ok}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
