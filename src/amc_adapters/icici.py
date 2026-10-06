from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
import uuid
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
    """ICICI Prudential monthly portfolio disclosures + digital factsheets.

    Monthly holdings come from the downloads REST API
    (``POST apimf.icicipruamc.com/nms/v1/downloads/files``). Two details are
    easy to get wrong and both fail silently rather than loudly:

    * the gateway rejects the POST with **405** unless the request carries
      ``env: api``, ``sourceurl: DOWNLOADS`` and ``requestapiid`` - a
      byte-identical payload without them is refused;
    * ``record['url']`` looks absolute but is only served under the
      ``https://www.icicipruamc.com/blob`` prefix. Fetching it from the API
      host 404s, and from the bare domain it returns the SPA shell.

    The response is a ZIP holding one XLSX per scheme, which is exactly what
    ``parse_zip``/``parse_excel`` consume. History goes back to 2013.

    ``discover_documents_all`` still returns the undated per-scheme factsheet
    PDFs, which are only used for returns/benchmark extraction.
    """

    API = "https://apimf.icicipruamc.com/nms/v1/downloads/files"
    BLOB = "https://www.icicipruamc.com/blob"
    # "Monthly Portfolio Disclosures" category id, from the site's own request.
    CATEGORY_ID = "26a073d7-08d2-4a95-95fa-f83a4ee51e40"

    def _api_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.icicipruamc.com/",
            "Origin": "https://www.icicipruamc.com",
            "env": "api",
            "sourceurl": "DOWNLOADS",
            "requestapiid": str(uuid.uuid4()),
        }

    async def discover_documents(
        self,
        portfolio_url: str,
        factsheet_url: str,
        target_month: int,
        target_year: int,
    ) -> list[PDFLink]:
        """The single monthly-disclosure ZIP for ``target_month``/``target_year``."""
        payload = {"categoryId": self.CATEGORY_ID, "schemeCategory": "",
                   "userType": "Investor", "fileType": "All", "page": "1",
                   "size": "100", "filter": [], "categoryName": "OTHERS"}
        async with httpx.AsyncClient(verify=False, timeout=45,
                                     headers=self._api_headers()) as client:
            resp = await client.post(self.API, json=payload)
            resp.raise_for_status()
            files = ((resp.json().get("success") or {}).get("data")
                     or {}).get("files") or []

        want = MONTH_NAMES[target_month - 1].lower()
        for rec in files:
            if "monthly" not in (rec.get("categoryName") or "").lower():
                continue
            title = (rec.get("title") or {}).get("text", "")
            # Trust the title's own month: the API's applicableMonth timestamp
            # is stamped inconsistently across years.
            if want not in title.lower():
                continue
            if str(target_year) not in title:
                continue
            rel = rec.get("url") or ""
            if not rel:
                continue
            url = self.BLOB + urllib.parse.quote(rel)
            fname = urllib.parse.unquote(rel.rsplit("/", 1)[-1])
            logger.info("ICICI %s disclosure -> %s", title, url)
            return [PDFLink(url=url, filename=fname, month=target_month,
                            year=target_year, scheme_name=title.strip())]

        logger.info("ICICI: no monthly portfolio disclosure published for %s-%s",
                    target_year, target_month)
        return []

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
