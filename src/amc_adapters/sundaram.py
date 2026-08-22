from __future__ import annotations

import logging
import re

import httpx

from .base import AMCAdapter, PDFLink, MONTH_NAMES, MONTH_ABBRS

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.sundarammutual.com/Monthly-Fortnightly-Adhoc-Portfolios"
API_URL = (
    "https://www.sundarammutual.com/ajax/"
    "Modules_Disclosure_Monthly_Fortnightly_Adhoc_Portfolios,"
    "App_Web_btvr3vbk.ashx?_method=GetCategory&_session=no"
)
FILE_BASE = "https://www.sundarammutual.com"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"


def _title_month_year(title: str) -> tuple[int | None, int | None]:
    """Parse "Monthly Portfolio Disclosure Equity & Fund of Funds - Jul 2026"."""
    year = None
    m = re.search(r"(\d{4})", title)
    if m:
        year = int(m.group(1))
    month = None
    for i, name in enumerate(MONTH_NAMES, 1):
        if name.lower() in title.lower():
            month = i
            break
    if month is None:
        for i, abbr in enumerate(MONTH_ABBRS, 1):
            if abbr.lower() in title.lower():
                month = i
                break
    return month, year


class SundaramAdapter(AMCAdapter):
    """Sundaram Mutual Fund monthly portfolio disclosures.

    The portfolio page calls an ASP.NET PageMethod (GetCategory) that returns
    HTML with links to monthly-portfolio XLSX files (one "Equity & Fund of
    Funds" + one "Fixed Income" per month, spanning years). Files carry full
    ISIN-level holdings.
    """

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": PAGE_URL,
        }
        links: list[PDFLink] = []
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=60, headers=headers, follow_redirects=True
            ) as client:
                for category in ("Monthly", "Fortnightly", "AdHoc"):
                    try:
                        resp = await client.post(API_URL, data=f"Catid={category}")
                        resp.raise_for_status()
                        html = resp.text
                    except Exception as e:
                        logger.debug(f"Sundaram {category} failed: {e}")
                        continue
                    # Links appear with \\' escaped single quotes in the response.
                    for href, title in re.findall(
                        r"<a href=\\'(/uploaddir/[^\\']+)\\'(?:[^>]*?)>(.*?)</a>",
                        html,
                        re.S,
                    ):
                        title = re.sub(r"<[^>]+>", "", title).strip()
                        if not href.lower().endswith((".xlsx", ".xls")):
                            continue
                        month, year = _title_month_year(title)
                        links.append(PDFLink(
                            url=FILE_BASE + href,
                            filename=href.rstrip("/").split("/")[-1],
                            month=month,
                            year=year,
                            scheme_name=title,
                            document_type="monthly_portfolio",
                        ))
        except Exception as e:
            logger.error(f"Sundaram discovery failed: {e}")

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
