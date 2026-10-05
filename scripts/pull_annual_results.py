"""Rebuild stock financial documents from NSE corporate-announcement PDFs.

Financial statements are sourced ONLY from the financial reports that listed
companies submit to SEBI and publish on the NSE under corporate actions: the
per-symbol corporate-announcements feed
`/api/corporate-announcements?index=equities&symbol={sym}&page={page}` and the
attached results PDFs (audited annual + unaudited quarterly). Each stock's
document in `data/stock_financials/` is BUILT FROM SCRATCH from those PDFs:

- history is whatever the announcement pages still serve (default 30 pages =
  several years of quarterly + annual results);
- both consolidated and standalone sections are populated when the PDFs exist;
- every record keeps its source URL + sha256, so figures trace back to the
  filed document;
- deterministic table parsing runs first; the AI vision tier (default the FREE
  opencode backend, see STMT_BACKEND in src/financial_statements.py) fills in
  pages determinism cannot read.

Usage:
    python scripts/pull_annual_results.py --limit 20
    python scripts/pull_annual_results.py --symbols KRBL,SBIN
    python scripts/pull_annual_results.py --no-ai        # deterministic only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import statement_schema as ss  # noqa: E402
from src.stock_common import (http_get, load_json, now_iso,  # noqa: E402
                              nse_session, save_json)
from src.stock_identity import load_identity  # noqa: E402
from src.financial_statements import (  # noqa: E402
    FINANCIALS_DIR, RAW_RESULTS_DIR, announce_url,
    _is_result_announcement, ai_extract_document, assemble,
    build_section_records, compute_ttm, download_pdf, parse_table_pages,
    parse_table_pages_guarded, sha256_file, validate_and_score,
)

SECTIONS = (("consolidated", "consolidated"), ("standalone", "standalone"),
            ("auto", "consolidated"))
STATUS_PATH = RAW_RESULTS_DIR / "annual_fy25_26_status.json"
INVENTORY_DIR = RAW_RESULTS_DIR / "_inventory"
ANNOUNCE_DIR = RAW_RESULTS_DIR / "_announcements"

_FR_HINT = re.compile(r"FR|Results|Outcome|Annual|Audited|Q4", re.I)
_NOISE_HINT = re.compile(r"Media|Presentation|Transcript|Clarif|Press[\s_-]"
                         r"Release|Newsletter|PPT", re.I)

_AUDITED_RE = re.compile(r"\baudited\b", re.I)
_UNAUDITED_RE = re.compile(r"\bunaudited\b", re.I)


def _audit_of(headline: str) -> str | None:
    """Filing class from the announcement headline. Word-boundary regexes:
    \\baudited\\b never matches inside 'unaudited'; a combined quarterly+annual
    filing that declares audited results ranks as Audited."""
    if _AUDITED_RE.search(headline or ""):
        return "Audited"
    if _UNAUDITED_RE.search(headline or ""):
        return "Unaudited"
    return None


def _audit_from_pdf(pdf) -> str | None:
    """Filing class from the results PDF text layer itself — most NSE
    headlines omit audited/unaudited, but the Reg-33 table header states it
    ('Unaudited Financial Results for the quarter ended...', 'Audited ...').
    Heuristic scan of the first two pages; Audited wins when both words
    appear (mirrors the combined-filing rule in _audit_of)."""
    try:
        import pymupdf as fitz
        with fitz.open(str(pdf)) as doc:
            text = " ".join(doc[i].get_text()
                            for i in range(min(2, doc.page_count)))
    except Exception:
        return None
    if _AUDITED_RE.search(text):
        return "Audited"
    if _UNAUDITED_RE.search(text):
        return "Unaudited"
    return None


def _url_rank(a: dict) -> int:
    name = a["url"].rsplit("/", 1)[-1]
    score = 0
    if _FR_HINT.search(name):
        score += 2
    if _NOISE_HINT.search(name):
        score -= 3
    return score


def _fetch_result_announcements(symbol: str, pages: int = 30) -> list[dict]:
    """Fresh feed pull of every financial-result announcement (audited/unaudited,
    quarterly/annual), keeping full attachment text (the shared helper truncates
    to 260 chars, which hides RELIANCE-style long titles)."""
    out: list[dict] = []
    seen: set[str] = set()
    for page_no in range(max(1, pages)):
        data = None
        for attempt in range(3):
            try:
                raw = http_get(announce_url(symbol, page_no),
                               headers={"Referer": "https://www.nseindia.com/"},
                               timeout=20, opener=nse_session(), retries=1)
                data = json.loads(raw.decode("utf-8", "replace"))
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1) ** 2)
        if data is None or not isinstance(data, list):
            continue
        fresh = False
        for a in data:
            url = a.get("attchmntFile") or ""
            if not url or url in seen or not url.lower().endswith(".pdf"):
                continue
            headline = (a.get("attchmntText") or "").strip()
            if not _is_result_announcement(headline):
                continue
            seen.add(url)
            fresh = True
            out.append({"date": (a.get("an_dt") or "")[:11],
                        "headline": headline[:800],
                        "url": url,
                        "rank": _url_rank({"url": url})})
        if not fresh:
            break
        if page_no + 1 < max(1, pages):
            time.sleep(0.4)
    out.sort(key=lambda a: (a["rank"], a["date"]), reverse=True)
    return out


def _substantive(recs: list[dict]) -> list[dict]:
    """Keep only records actually carrying financial substance — never write
    empty shells that would blank out a table column (bank PDFs parse to
    nothing under the canonical schema)."""
    keys = ("revenue_from_operations", "total_income", "pat", "pbt")
    return [r for r in recs
            if any(r.get(k) is not None and (r.get(k) or 0) != 0 for k in keys)]


def _in_bounds(new: float | None, old: float | None,
               pat: bool = False) -> bool:
    if new is None or old is None or (old or 0) == 0:
        return True
    ratio = new / old
    if (new < 0) != (old < 0):
        return True                            # loss<->profit swing: pass
    return (0.1 <= ratio <= 10.0) if pat else (0.25 <= ratio <= 4.0)


def _sanity_gate_in_set(annuals: list[dict]) -> tuple[list[dict], int]:
    """Reject annual records whose revenue/PAT magnitude is implausible versus
    the previous year parsed for the SAME stock (keeps AI misreads like
    'pat 315.8 vs 26,634' from ever reaching the document)."""
    record_list = sorted(annuals, key=lambda r: r.get("period_end") or "")
    kept: list[dict] = []
    rejected = 0
    for r in record_list:
        ok = True
        if kept:
            prev = kept[-1]
            nrev = r.get("revenue_from_operations") or r.get("total_income")
            prev_rev = prev.get("revenue_from_operations") \
                or prev.get("total_income")
            if not _in_bounds(nrev, prev_rev) \
                    or not _in_bounds(r.get("pat"), prev.get("pat"), pat=True):
                ok = False
        if ok:
            kept.append(r)
        else:
            rejected += 1
    return kept, rejected


def _year_hint(headline: str) -> str:
    per = ss.primary_period_from_headline(headline)
    if not per:
        return "?"
    kind, d = per
    if isinstance(d, tuple) and len(d) == 3:
        return f"{kind} {d[0]}-{d[1]:02d}-{d[2]:02d}"
    return f"{kind} {d}"


# Filings whose declared period ends before 1 Apr 2021 sit outside the
# last-five-years window (FY2021-22 .. FY2025-26); the vision tier skips them.
AI_WINDOW_FLOOR = (2021, 4)


def _in_ai_window(headline: str) -> bool:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s*$", _year_hint(headline))
    if not m:
        return True                      # unknown period: give the AI a chance
    return (int(m.group(1)), int(m.group(2))) >= AI_WINDOW_FLOOR


def _save_inventory(isin: str, symbol: str, inv: list[dict]) -> None:
    """Sidecar per symbol: every result announcement considered, with its
    filing class, declared period, download/parse outcome — the raw material
    for the audited/unaudited coverage report."""
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    save_json(INVENTORY_DIR / f"{symbol}.json", {
        "isin": isin, "symbol": symbol, "fetched_at": now_iso(),
        "announcements": inv,
    })


def build_one(isin: str, row: dict, ai: bool = True, ai_pages: int = 24,
              max_docs: int = 90, pages: int = 30,
              local: bool = False) -> dict:
    """Download the stock's corporate-announcement result PDFs and rebuild its
    document from scratch. Returns the sidecar status row.

    local=True reads the feed snapshot + local PDFs written by
    scripts/download_results.py — zero NSE calls (offline parse phase)."""
    symbol = row.get("symbol") or ""
    st = {"isin": isin, "symbol": symbol, "status": "missing_doc"}
    if not symbol:
        return st
    if local:
        snap = load_json(ANNOUNCE_DIR / f"{symbol}.json")
        if not snap:
            st["status"] = "no_snapshot"
            print(f"[{symbol}] no feed snapshot — run "
                  f"scripts/download_results.py first", flush=True)
            return st
        anns = [a for a in (snap.get("announcements") or [])
                if a.get("downloaded") and a.get("path")
                and Path(a["path"]).exists()]
        print(f"[{symbol}] {isin} local snapshot: {len(anns)} result PDFs",
              flush=True)
        if not anns:
            st["status"] = "no_downloads"
            _save_inventory(isin, symbol, [])
            return st
    else:
        print(f"[{symbol}] {isin} scanning {pages} announcement pages...",
              flush=True)
        anns = _fetch_result_announcements(symbol, pages=pages)
        anns = anns[:max_docs]
        if not anns:
            st["status"] = "no_result_announcements"
            _save_inventory(isin, symbol, [])
            print(f"[{symbol}] none", flush=True)
            return st
    print(f"[{symbol}] {len(anns)} result PDFs in scope", flush=True)

    inventory: list[dict] = []
    all_raw: list[dict] = []
    sources: list[dict] = []
    for a in anns:
        audit = a.get("audit") if local else None
        if not audit:
            audit = _audit_of(a["headline"])
        year_hint = _year_hint(a["headline"])
        inv = {"date": a["date"], "headline": a["headline"][:300],
               "url": a["url"], "audit": audit, "period": year_hint,
               "downloaded": False, "rows": 0, "tier": None}
        if local:
            pdf = Path(a["path"])
            if pdf.stat().st_size <= 1024:
                inventory.append(inv)
                continue
        else:
            pdf = download_pdf(a["url"], symbol)
            if not pdf:
                print(f"  [{symbol}] FAIL download "
                      f"{a['url'].rsplit('/', 1)[-1][:55]}", flush=True)
                inventory.append(inv)
                continue
        inv["downloaded"] = True
        if not audit:
            audit = _audit_from_pdf(pdf)
            inv["audit"] = audit
        print(f"  [{symbol}] {a['url'].rsplit('/', 1)[-1][:55]} "
              f"year={year_hint} audit={audit or '?'} ({a['date']})", flush=True)
        size_mb = pdf.stat().st_size / 1048576
        oversized = pdf.stat().st_size > 12 * 1024 * 1024
        if oversized and not ai:
            # result filings are small; multi-MB blobs are newspaper ads /
            # annual-report mailers that only burn the parse budget
            print(f"  [{symbol}]   -> skipped (oversized "
                  f"{size_mb:.1f} MB)", flush=True)
            inventory.append(inv)
            continue
        before = len(all_raw)
        t_pdf = time.time()
        tiers: list[str] = []
        det: list[dict] = []
        if not oversized:
            det = parse_table_pages_guarded(
                pdf, primary_period=ss.primary_period_from_headline(a["headline"])
            ) or []
            if det:
                for r in det:
                    r["_audit"] = audit
                    r["_src"] = {"url": a["url"], "date": a["date"]}
                all_raw.extend(det)
                tiers.append("det")
        subst = any(
            r.get("canon") in ("revenue_from_operations", "total_income",
                               "pat", "pbt", "profit_before_tax")
            and any(v not in (None, 0) for v in (r.get("values") or {}).values())
            for r in det)
        # AI vision fallback (free opencode backend by default; per-page
        # payloads cached by file sha256): fills the deterministic parser's
        # honest gaps — scanned/image-only pages and budget-timeout vector
        # soup — in the SAME internal row shape. Triggered per document
        # whenever determinism found nothing substantive; oversized PDFs go
        # AI-only (they would burn the det budget), and filings whose
        # declared period predates the 5-year window stay unparsed.
        if ai and (not det or not subst) and _in_ai_window(a["headline"]):
            raw_ai = ai_extract_document(pdf, max_pages=ai_pages)
            if raw_ai:
                for r in raw_ai:
                    r["_audit"] = audit
                    r["_src"] = {"url": a["url"], "date": a["date"]}
                all_raw.extend(raw_ai)
                tiers.append("ai")
        added = len(all_raw) - before
        inv["rows"] = added
        inv["tier"] = "+".join(tiers) if tiers else None
        inventory.append(inv)
        print(f"  [{symbol}]   -> {('+'.join(tiers)) if tiers else 'no rows'} "
              f"({added} raw rows, {time.time()-t_pdf:.0f}s)", flush=True)
        if tiers:
            sources.append({"url": a["url"], "date": a["date"],
                            "sha256": sha256_file(pdf),
                            "tier": "+".join(tiers)})
    _save_inventory(isin, symbol, inventory)
    if not sources:
        st["status"] = "no_downloads"
        return st

    smap = build_section_records(all_raw)
    doc: dict = {"isin": isin, "symbol": symbol, "fetched_at": now_iso()}
    merged_any = False
    sanity_rejected = 0
    sections: dict[str, dict] = {}
    for section_name, section_key in SECTIONS:
        sec_map = smap.get(section_name)
        if not sec_map:
            continue
        quarters, annuals = assemble(sec_map)
        quarters = _substantive(quarters)
        annuals = _substantive(annuals)
        annuals, rejected = _sanity_gate_in_set(annuals)
        sanity_rejected += rejected
        if not quarters and not annuals:
            continue
        block = {"quarters": quarters, "annual": annuals,
                 "ttm": compute_ttm(quarters)}
        sections[section_key] = block
        merged_any = True
    if not merged_any:
        st["status"] = "no_matching_records"
        return st

    doc.update(sections)
    doc["sources"] = sources
    doc["validation"] = {"issues": [], "confidence": 0,
                         "checked_at": now_iso()}

    section_confs: list[int] = []
    for sk in ("consolidated", "standalone"):
        block = doc.get(sk)
        if not block:
            continue
        _, c = validate_and_score(list(block.get("quarters") or []),
                                  list(block.get("annual") or []))
        section_confs.append(c)
    primary = doc.get("consolidated") or doc.get("standalone")
    issues, conf = validate_and_score(list(primary.get("quarters") or []),
                                      list(primary.get("annual") or []))
    overall = max([conf] + section_confs) if section_confs else conf
    doc["validation"] = {"issues": issues, "confidence": overall,
                         "checked_at": now_iso()}
    save_json(FINANCIALS_DIR / f"{isin}.json", doc)

    fy_counts = {
        sk: len([1 for r in (doc.get(sk) or {}).get("annual") or []
                 if r.get("kind") == "FY"])
        for sk in ("consolidated", "standalone")}
    st.update({
        "status": "built",
        "documents": len(sources),
        "sanity_rejected": sanity_rejected,
        "consolidated_fy": fy_counts.get("consolidated", 0),
        "standalone_fy": fy_counts.get("standalone", 0),
        "confidence": overall,
    })
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated subset")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-docs", type=int, default=90,
                    help="max result PDFs to download per stock")
    ap.add_argument("--pages", type=int, default=30,
                    help="announcement-feeds pages to scan back")
    ap.add_argument("--ai-pages", type=int, default=24,
                    help="PDF pages to offer the AI vision tier per document")
    ap.add_argument("--sleep", type=float, default=1.2,
                    help="seconds between symbols (NSE throttle)")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--refresh-status", action="store_true",
                    help="ignore sidecar and rebuild even handled symbols")
    ap.add_argument("--from-local", action="store_true",
                    help="parse from local feed snapshots + downloaded PDFs "
                         "(scripts/download_results.py output); no NSE calls")
    ap.add_argument("--status-path", default=str(STATUS_PATH),
                    help="sidecar status file (per-worker paths let several "
                         "batches run in parallel without clobbering)")
    args = ap.parse_args()

    status_path = Path(args.status_path)
    ident = load_identity()
    want = {s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()}
    if args.from_local and not want:
        # offline universe = every symbol that has a feed snapshot
        by_sym = {v.get("symbol", "").upper(): k for k, v in ident.items()
                  if v.get("symbol")}
        want = {p.stem.upper() for p in ANNOUNCE_DIR.glob("*.json")
                if p.stem.upper() in by_sym}
    status = load_json(status_path) or {}
    done = set(status) if not args.refresh_status else set()
    selected = [i for i, r in ident.items()
                if (not want or r.get("symbol", "").upper() in want)]
    if args.limit:
        selected = selected[:args.limit]

    totals: dict[str, int] = {}
    count = len(selected)
    for i, isin in enumerate(selected, 1):
        if isin in done:
            continue
        row = ident[isin]
        st = build_one(isin, row, ai=not args.no_ai,
                       ai_pages=args.ai_pages, max_docs=args.max_docs,
                       pages=args.pages, local=args.from_local)
        status[isin] = st
        totals[st.get("status", "?")] = totals.get(st.get("status", "?"), 0) + 1
        line = f"[{i}/{count}] {isin} {row.get('symbol','?'):<8} {st['status']:<26}"
        if st.get("status") == "built":
            line += (f" docs={st['documents']} conf={st['confidence']} "
                     f"consFY={st['consolidated_fy']}"
                     f" stdFY={st['standalone_fy']}")
        print(line, flush=True)
        save_json(status_path, status)
        if not args.from_local:
            time.sleep(args.sleep)
    save_json(status_path, status)
    print("totals:", json.dumps(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())