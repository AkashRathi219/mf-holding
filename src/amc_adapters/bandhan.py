from __future__ import annotations

import logging
import re

import httpx

from .base import AMCAdapter, PDFLink

logger = logging.getLogger(__name__)

CMS_URL = "https://cmsnew.bandhanmutual.com/wp-json/finance-api/v1/posts/disclosures?posts_per_page=2500"

# e.g. "Monthly portfolio as at 31-Jul-2021", "15 April 2025", "31-07-2021"
DATE_PATTERNS = [
    re.compile(r"(\d{1,2})[-/\s]+([A-Za-z]{3,})[-/\s]+(\d{4})"),
    re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})"),
]
MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _parse_date(text: str) -> tuple[int, int] | None:
    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g1, g2, g3 = m.group(1), m.group(2), m.group(3)
        if pat.pattern.endswith("(\\d{4})"):
            month_token = g2.lower()[:3]
            month = MONTH_MAP.get(month_token)
            if month:
                return (month, int(g3))
        else:
            try:
                return (int(g2), int(g3))
            except ValueError:
                return None
    return None


class BandhanAdapter(AMCAdapter):
    """Bandhan publishes disclosure documents (incl. monthly portfolios) through
    its WordPress finance API hosted on cmsnew.bandhanmutual.com."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=60,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
                    "Referer": "https://bandhanmutual.com/",
                },
            ) as client:
                resp = await client.get(CMS_URL)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error(f"Bandhan CMS API failed: {e}")
            return []

        posts = data.get("data", []) if isinstance(data, dict) else data

        links: list[PDFLink] = []
        for post in posts or []:
            acf = post.get("acf_fields") or {}
            doc_type = acf.get("download_center_type", "")
            files = acf.get("disclosure_files") or []
            is_portfolio = "portfolio" in doc_type.lower() or "investments made" in doc_type.lower()
            for fl in files:
                name = fl.get("document_name", "") or ""
                url = (fl.get("document_link") or {}).get("url", "")
                if not url:
                    continue
                if is_portfolio or "monthly portfolio" in name.lower():
                    month, year = None, None
                    detected = _parse_date(name + " " + url)
                    if detected:
                        month, year = detected
                    filename = url.rstrip("/").split("/")[-1].split("?")[0]
                    links.append(PDFLink(
                        url=url,
                        filename=filename,
                        month=month,
                        year=year,
                        scheme_name=name,
                    ))

        return links
