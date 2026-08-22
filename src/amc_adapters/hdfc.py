from __future__ import annotations

import asyncio
import json
import logging
import re

from .base import AMCAdapter, MONTH_NAMES, PDFLink

logger = logging.getLogger(__name__)

API_URL = "https://cms.hdfcfund.com/en/hdfc/api/v2/disclosures/monthfortportfolio"
FACTSHEET_PAGE = "https://www.hdfcfund.com/mutual-funds/factsheets"

HEADERS = {
    "Origin": "https://www.hdfcfund.com",
    "Referer": "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio",
}

MONTHS = list(range(1, 13))


def _month_number(month_name: str) -> int | None:
    for i, name in enumerate(MONTH_NAMES, 1):
        if name in (month_name or "").lower():
            return i
    return None


def _extract_factsheet_links(html: str) -> list[PDFLink]:
    """Parse the factsheets page's ``__NEXT_DATA__`` for document links.

    HDFC publishes a consolidated grouped factsheet (``HDFC MF Factsheet -
    <Month> <Year>.pdf``) plus an index-solutions factsheet every month.  Both
    are multi-scheme PDFs; ``src/pdf_agents`` splits them into per-scheme
    records downstream.
    """
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        resp = data["props"]["pageProps"]["factSheetResponse"]["data"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    links: list[PDFLink] = []
    latest = (resp.get("latestInvestorsDocuments") or [{}])[0]
    try:
        ly = int(latest.get("latestYear") or 0)
        lm = _month_number(latest.get("latestMonth") or "")
    except (TypeError, ValueError):
        ly, lm = 0, None
    if not ly or not lm:
        return []

    for key in ("factsheetfile", "indexFactsheetFile"):
        for item in latest.get(key) or []:
            path = (item or {}).get("path", "")
            title = (item or {}).get("title", "")
            if not path:
                continue
            filename = path.rstrip("/").split("/")[-1].split("?")[0]
            links.append(PDFLink(
                url=path,
                filename=filename,
                month=lm,
                year=ly,
                scheme_name=title,
                document_type="factsheet",
            ))
    return links


class HDFCAdapter(AMCAdapter):
    """HDFC's Akamai WAF blocks plain HTTP clients, but Chrome TLS impersonation
    (curl_cffi) passes. Files are listed via the cms.hdfcfund.com disclosure API
    and served from files.hdfcfund.com (downloadable with plain httpx).  The
    factsheets page (Next.js ``__NEXT_DATA__``) provides the consolidated
    monthly factsheet, which the grouped-PDF splitter turns into per-scheme
    records."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            from curl_cffi import requests as cr
        except ImportError:
            logger.warning("curl_cffi not installed; HDFC adapter disabled")
            return []

        links: list[PDFLink] = []

        def query(year: int, month: int) -> list[dict]:
            try:
                resp = cr.post(
                    API_URL,
                    impersonate="chrome",
                    timeout=30,
                    headers=HEADERS,
                    data={"year": str(year), "type": "monthly", "month": str(month)},
                )
                resp.raise_for_status()
                data = resp.json()
                return (data.get("data", {}).get("files")) or []
            except Exception as e:
                logger.debug(f"HDFC API ({year}-{month}) failed: {e}")
                return []

        # Query recent years x all months to also provide fallback coverage.
        for year in (2026, 2025, 2024):
            for month in MONTHS:
                files = await asyncio.to_thread(query, year, month)
                for item in files:
                    title = item.get("title", "") or ""
                    url = (item.get("file") or {}).get("url", "")
                    if not url or "Monthly" not in title:
                        continue
                    # month/year already known from the query params.
                    filename = url.rstrip("/").split("/")[-1].split("?")[0]
                    links.append(PDFLink(
                        url=url,
                        filename=filename,
                        month=month,
                        year=year,
                        scheme_name=title,
                    ))

        # Consolidated factsheets from the factsheets page (latest month).
        try:
            page = await asyncio.to_thread(
                cr.get, FACTSHEET_PAGE, impersonate="chrome",
                timeout=30, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.hdfcfund.com/",
                },
            )
            page.raise_for_status()
            links.extend(_extract_factsheet_links(page.text))
        except Exception as e:
            logger.debug(f"HDFC factsheet page failed: {e}")

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
