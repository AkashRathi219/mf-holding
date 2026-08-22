from __future__ import annotations

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from .base import AMCAdapter, PDFLink, MONTH_NAMES, MONTH_ABBRS

logger = logging.getLogger(__name__)

# Kotak's factsheets are served on the bank's distributor portal (the AMC's own
# kotakmf.com is behind Radware PerfDrive + hCaptcha). The portal has two entry
# pages (equity / debt) linking to per-category pages, each of which links to
# individual scheme factsheet pages (one HTML page per scheme).
ENTRY_PAGES = [
    "https://www.kotak.bank.in/MF_Factsheet/equity.html",
    "https://www.kotak.bank.in/MF_Factsheet/debts.html",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"


def _headers() -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
    }


def _parse_asof_date(html: str) -> tuple[int | None, int | None]:
    """Parse "Factsheet as on April 30, 2026" into (month, year)."""
    m = re.search(r"Factsheet as on\s+([^<]+?)<", html, re.IGNORECASE)
    text = m.group(1).strip() if m else ""
    if not text:
        return None, None
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
    ym = re.search(r"(\d{4})", text)
    if ym:
        year = int(ym.group(1))
    return month, year


class KotakAdapter(AMCAdapter):
    """Kotak Mahindra MF factsheets from the kotak.bank.in distributor portal.

    The portal lists factsheets for many AMCs; we filter to scheme pages whose
    filename starts with "Kotak". Each scheme page is a self-contained HTML
    factsheet (top-10 holdings table etc.) for the current month - the portal
    has no historical archive. The AMC's own kotakmf.com is protected by Radware
    PerfDrive + hCaptcha, so this portal is the reliable source.
    """

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        async with httpx.AsyncClient(
            verify=False, timeout=30, headers=_headers(), follow_redirects=True
        ) as client:
            # 1. Entry pages -> category pages
            category_pages: list[str] = []
            for entry in ENTRY_PAGES:
                try:
                    resp = await client.get(entry)
                    resp.raise_for_status()
                except Exception as e:
                    logger.debug(f"Kotak entry fetch failed ({entry}): {e}")
                    continue
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.endswith(".html") and href not in (
                        "equity.html", "debts.html",
                        "EQUITY/scheme-pages/disclaimer.html",
                        "EQUITY/scheme-pages/glossaries.html",
                        "Debt/scheme-pages/disclaimer.html",
                        "Debt/scheme-pages/glossaries.html",
                    ):
                        category_pages.append(href)

            # 2. Category pages -> scheme factsheet pages (Kotak only)
            scheme_pages: list[tuple[str, str]] = []  # (url, title)
            for cat in sorted(set(category_pages)):
                if not cat.startswith("http"):
                    cat = "https://www.kotak.bank.in/MF_Factsheet/" + cat
                try:
                    resp = await client.get(cat)
                    resp.raise_for_status()
                except Exception as e:
                    logger.debug(f"Kotak category fetch failed ({cat}): {e}")
                    continue
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"] or ""
                    if "scheme-pages/" not in href or not href.endswith(".html"):
                        continue
                    filename = href.rstrip("/").split("/")[-1]
                    if not filename.lower().startswith("kotak"):
                        continue
                    title = a.get_text(strip=True)
                    if not href.startswith("http"):
                        from urllib.parse import urljoin
                        href = urljoin(cat, href)
                    scheme_pages.append((href, title))

            # 3. Resolve month/year + scheme name from each scheme page.
            links: list[PDFLink] = []

            async def resolve(page: str, title: str) -> PDFLink | None:
                try:
                    resp = await client.get(page)
                    resp.raise_for_status()
                except Exception as e:
                    logger.debug(f"Kotak scheme fetch failed ({page}): {e}")
                    return None
                month, year = _parse_asof_date(resp.text)
                scheme_name = title or page.rstrip("/").split("/")[-1]
                filename = scheme_name.strip().replace(" ", "-") + ".html"
                return PDFLink(
                    url=page,
                    filename=filename,
                    month=month,
                    year=year,
                    scheme_name=scheme_name,
                )

            resolved = await asyncio.gather(
                *[resolve(*pair) for pair in dict.fromkeys(scheme_pages)]
            )
            links = [link for link in resolved if link]

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
