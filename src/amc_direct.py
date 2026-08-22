"""AMC-direct portfolio link capture + PDF download.

Each AMC rotates its own monthly portfolio-disclosure URLs every month, so the
links resolved for a target month are PERSISTED before download
(``data/logs/portfolio_links/{YYYY-MM}.json``) - provenance plus a re-download
manifest. Advisorkhoj's page is stable by contrast and stays enabled in the
background as the fallback source; anything captured here lands in
``data/raw/pdfs/{AMC}/{Y}/{MM}/`` where the existing ingest/parse flow tags it
``source='amc_website'``, which outranks the background aggregator on merge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = BASE_DIR / "config" / "amc_registry.json"
LINKS_DIR = BASE_DIR / "data" / "logs" / "portfolio_links"

# Monthly disclosures arrive late; accept target month minus up to 2 months so
# an early-next-month run still catches the prior cycle, but never years-old
# artifacts. Undated links are kept for the download step to place by target.
RECENCY_MONTHS = 2


def _now_ist() -> str:
    from src.refresh_log import _now_ist
    return _now_ist()


def load_registry() -> list[dict]:
    with open(REGISTRY_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def links_path(year: int, month: int) -> Path:
    return LINKS_DIR / f"{year}-{month:02d}.json"


def _within_recency(year: int, month: int, link_year, link_month) -> bool:
    if link_year is None or link_month is None:
        return True
    idx_t = year * 12 + month
    idx_l = link_year * 12 + link_month
    return idx_t - RECENCY_MONTHS <= idx_l <= idx_t


def _is_relevant_document(filename: str) -> bool:
    from main import _is_relevant_document  # reuse the pipeline filter
    return _is_relevant_document(filename)


_DOC_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".csv", ".zip")
_KEYWORDS = ("portfolio", "factsheet", "monthly")
_MONTH_TOKENS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)?\d{2})(?!\d)")
_NUM_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[-._/](\d{1,2})[-._/](\d{2,4})(?!\d)")


def _self_date(text: str) -> tuple[int, int] | None | str:
    """Date hidden in a filename/URL, independent of adapter stamping.

    Returns (month, year), the string 'undated' when no date signal appears
    at all, or None when a signal exists but yields nothing usable
    (ambiguous - treat as not current)."""
    t = text.lower()
    # Numeric dd-mm-yyyy / dd.mm.yyyy (Indian convention: middle = month).
    m = _NUM_DATE_RE.search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            if y < 100:
                y += 2000
            return (mo, y)
    # Month-name based ('...august-2026...', 'apr-19', 'sept_30_2019').
    for tok, mnum in _MONTH_TOKENS.items():
        idx = t.find(tok)
        if idx == -1:
            continue
        window = t[idx + len(tok): idx + len(tok) + 12]
        ym = re.search(r"((?:19|20)\d{2})", window) or _YEAR_RE.search(window)
        if not ym:
            return None
        y = int(ym.group(1))
        if y < 100:
            y += 2000
        return (mnum, y)
    return "undated"


def _keep_link(link, year: int, month: int) -> bool:
    """Stricter than the pipeline filter: this module builds the per-month
    download manifest, so only real documents qualify. The adapters stamp
    undated links with the target month, so their month/year fields cannot
    be trusted here - dates are re-derived from the filename/URL itself."""
    fn = (link.filename or "").strip().lower()
    if not fn.endswith(_DOC_EXTENSIONS):
        return False
    if not _is_relevant_document(fn):
        return False
    hint = _self_date(f"{fn} {(link.url or '').lower()}")
    if hint == "undated":
        # No date signal anywhere: accept portfolio/factsheet-style names as
        # current-month docs; drop everything else (forms, policies, guides).
        return any(k in fn for k in _KEYWORDS)
    if hint is None:
        return False
    return _within_recency(year, month, hint[1], hint[0])


async def discover_amc_links(amc: dict, year: int, month: int) -> dict:
    """Resolve one AMC's own document links for the target month."""
    from src.amc_adapters import get_adapter

    amc_name = amc["mf_name"]
    portfolio_url = amc.get("amc_monthly_portfolio_disclosure", "")
    factsheet_url = amc.get("amc_monthly_mf_factsheets", "")
    entry: dict = {
        "mf_id": amc.get("mf_id"),
        "mf_name": amc_name,
        "portfolio_url": portfolio_url,
        "factsheet_url": factsheet_url,
        "status": "no_urls",
        "target_month": month,
        "target_year": year,
        "links": [],
    }
    if not portfolio_url and not factsheet_url:
        return entry

    adapter = get_adapter(amc_name)
    doc_links = await adapter.discover_documents(
        portfolio_url=portfolio_url,
        factsheet_url=factsheet_url,
        target_month=month,
        target_year=year,
    )
    doc_links = [l for l in doc_links if _within_recency(year, month, l.year, l.month)]

    seen: set[str] = set()
    entry["links"] = []
    for l in doc_links:
        if l.url in seen or not _keep_link(l, year, month):
            continue
        seen.add(l.url)
        # Trust only self-detected dates for placement (adapter fields stamp
        # undated links with the target month).
        text = f"{(l.filename or '').lower()} {(l.url or '').lower()}"
        hint = _self_date(text)
        d_month, d_year = hint if isinstance(hint, tuple) else (month, year)
        entry["links"].append({
            "filename": l.filename,
            "url": l.url,
            "disclosure_month": d_month,
            "disclosure_year": d_year,
            "document_type": l.document_type,
        })
    entry["status"] = "ok" if entry["links"] else "not_found"
    return entry


def save_links(year: int, month: int, amc_filter: str | None = None) -> dict:
    """Discover + persist every registry AMC's own links for the given month."""
    amcs = load_registry()
    if amc_filter:
        amcs = [a for a in amcs if amc_filter.lower() in a["mf_name"].lower()]
        if not amcs:
            raise SystemExit(f"No AMC found matching '{amc_filter}'")

    async def _all():
        return [await discover_amc_links(a, year, month) for a in amcs]

    entries = asyncio.run(_all())

    out = links_path(year, month)
    # Merge: a filtered run must not erase other AMCs already captured for
    # this month.
    existing: dict[str, dict] = {}
    if out.exists():
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
            if prior.get("target_year") == year and prior.get("target_month") == month:
                existing = {e["mf_name"]: e for e in prior.get("amcs", [])}
        except Exception:
            existing = {}
    for e in entries:
        existing[e["mf_name"]] = e

    doc = {
        "generated_at": _now_ist(),
        "target_year": year,
        "target_month": month,
        "amcs": sorted(existing.values(), key=lambda e: e["mf_name"].lower()),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")

    ok = sum(1 for e in existing.values() if e["status"] == "ok")
    n_links = sum(len(e["links"]) for e in existing.values())
    logger.info(f"Saved {n_links} link(s) across {ok}/{len(existing)} AMCs -> {out}")
    return {"path": str(out), "discovered_now": len(entries),
            "with_links": ok, "links": n_links}


async def _download_all(year: int, month: int, amc_filter: str | None) -> dict:
    from src.pdf_downloader import DocumentDownloader

    path = links_path(year, month)
    if not path.exists():
        raise SystemExit(
            f"No saved links for {year}-{month:02d} at {path}; "
            f"run 'portfolio-links --year {year} --month {month}' first")
    doc = json.loads(path.read_text(encoding="utf-8"))

    config_pdfs = BASE_DIR / "data" / "raw" / "pdfs"
    downloader = DocumentDownloader(output_dir=config_pdfs)

    summary = {"link_file": str(path), "downloaded": [], "already_present": 0,
               "failed": [], "skipped_amcs": []}
    for entry in doc["amcs"]:
        name = entry["mf_name"]
        if amc_filter and amc_filter.lower() not in name.lower():
            continue
        if entry["status"] != "ok" or not entry["links"]:
            summary["skipped_amcs"].append(name)
            continue
        for l in entry["links"]:
            expected = downloader.get_output_path(
                name, l["disclosure_year"], l["disclosure_month"], l["filename"])
            cached = expected.exists() and expected.stat().st_size > 0
            out_path = await downloader.download_file(
                url=l["url"], amc_name=name,
                year=l["disclosure_year"], month=l["disclosure_month"],
                filename=l["filename"])
            if out_path is None:
                summary["failed"].append(f"{name}:{l['filename']}")
            elif cached:
                summary["already_present"] += 1
            else:
                summary["downloaded"].append(
                    out_path.relative_to(config_pdfs).as_posix())
    return summary


def download_saved(year: int, month: int, amc_filter: str | None = None) -> dict:
    """Download PDFs listed in the saved links file into data/raw/pdfs/.

    Files land under {AMC}/{YYYY}/{MM}/ using each link's DETECTED disclosure
    month (target month when undated), so `parse-batch`/`ingest` attribute the
    right as-of period without extra flags."""
    return asyncio.run(_download_all(year, month, amc_filter))
