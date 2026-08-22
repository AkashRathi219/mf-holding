from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import httpx

from .base import AMCAdapter, PDFLink, MONTH_NAMES, MONTH_ABBRS

logger = logging.getLogger(__name__)

# "The Prudent Fact Sheet" - per-scheme digital factsheets (current month only).
ACTIVE_URL = "https://digitalfactsheet.icicipruamc.com/fact/"
PASSIVE_URL = "https://digitalfactsheet.icicipruamc.com/passive/"

# Index pages that are not individual schemes (navigation / annexure pages).
_NON_SCHEME_PAGES = {
    "index.php",
    "economic-overview.php",
    "economic-overview-and-market-outlook.php",
    "market-review-and-market-outlook.php",
    "annexure-of-quantitative-indicators-for-debt-fund.php",
    "annexure-of-quantitative-indicators-debt-etf-index-schemes.php",
    "annexure-for-all-potential-risk-class.php",
    "annexure-for-methodology-of-all-index-funds-and-etf-schemes.php",
    "fund-details-annexure.php",
    "annexure-for-returns-of-all-the-schemes.php",
    "annexure-for-returns-of-all-the-schemes-direct-plan.php",
    "fund-manager-detail.php",
    "annexure-i.php",
    "annexure-ii.php",
    "idcw-history-for-all-schemes.php",
    "investment-objective-of-all-the-schemes.php",
    "schedule-1-one-liner-definitions.php",
    "schedule-2-how-to-read-factsheet.php",
    "statutory-details-and-risk-factors.php",
    "systematic-investment-plan-sip-of-select-schemes.php",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"


def _headers() -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
    }


def _parse_month_year(header_sub_heading: str) -> tuple[int | None, int | None]:
    """Parse "July 31, 2026" style header into (month, year)."""
    if not header_sub_heading:
        return None, None
    text = header_sub_heading.strip()
    month, year = None, None
    for i, name in enumerate(MONTH_NAMES, 1):
        if name.lower() in text.lower():
            month = i
            break
    if month is None:
        for i, abbr in enumerate(MONTH_ABBRS, 1):
            if abbr.lower() in text.lower():
                month = i
                break
    m = re.search(r"(\d{4})", text)
    if m:
        year = int(m.group(1))
    return month, year


class ICICIAdapter(AMCAdapter):
    """ICICI Prudential "Prudent Fact Sheet" digital factsheets.

    The legacy downloads REST API lists monthly portfolio disclosure ZIPs, but
    the actual files live on `archive.icicipruamc.com`, which has no DNS
    A-record on this network. Instead we scrape the digital factsheet site
    (active + passive), which serves one factsheet PDF per scheme for the
    current month, directly downloadable with plain httpx.
    """

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        links: list[PDFLink] = []

        async def fetch_index(base: str) -> tuple[str, str] | None:
            try:
                async with httpx.AsyncClient(
                    verify=False, timeout=30, headers=_headers(), follow_redirects=True
                ) as client:
                    resp = await client.get(base)
                    resp.raise_for_status()
                    return resp.text, base
            except Exception as e:
                logger.debug(f"ICICI index fetch failed ({base}): {e}")
                return None

        for base in (ACTIVE_URL, PASSIVE_URL):
            fetched = await fetch_index(base)
            if not fetched:
                continue
            html, base_url = fetched

            # Header carries the as-of month/year for the whole site.
            m = re.search(r"header_sub_heading\">([^<]+)<", html)
            month, year = _parse_month_year(m.group(1) if m else "")

            # Every scheme page has href="<slug>.php" class="sub-item".
            for href, title in re.findall(
                r'<a href="([a-z0-9\-]+\.php)" class="sub-item">([^<]+)</a>', html
            ):
                slug = href
                if slug in _NON_SCHEME_PAGES:
                    continue
                filename = slug.replace(".php", ".pdf")
                url = base_url + "pdf/" + filename
                links.append(PDFLink(
                    url=url,
                    filename=filename,
                    month=month,
                    year=year,
                    scheme_name=title.strip(),
                ))

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
