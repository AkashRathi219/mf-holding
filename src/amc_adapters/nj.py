from __future__ import annotations

import logging
from urllib.parse import unquote, urljoin

from src.utils import get_random_headers

from .base import AMCAdapter, PDFLink, extract_month_year
from .playwright_adapter import LINK_FILTER_JS

logger = logging.getLogger(__name__)

MONTHLY_URL = "https://downloads.njmutualfund.com/njmf_download.php?nme=127"


class NJAdapter(AMCAdapter):
    """NJ publishes consolidated monthly portfolio disclosures as XLS/XLSX files
    on downloads.njmutualfund.com (links like viewfile.php?file=Monthly-...)."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        url = MONTHLY_URL
        raw: list[tuple[str, str]] = []

        # Fast path: plain HTTP.
        try:
            import httpx
            from bs4 import BeautifulSoup

            async with httpx.AsyncClient(
                verify=False, timeout=30, follow_redirects=True,
                headers={**get_random_headers(), "Referer": "https://downloads.njmutualfund.com/"},
            ) as client:
                resp = await client.get(url)
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "viewfile.php" in href and "Monthly" in href:
                        href = self._resolve(href, url)
                        raw.append((href, a.get_text(strip=True)))
        except Exception as e:
            logger.debug(f"NJ HTTP scrape failed: {e}")

        # Fallback: Playwright.
        if not raw:
            try:
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    try:
                        context = await browser.new_context(
                            user_agent=(
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0.0.0 Safari/537.36"
                            )
                        )
                        page = await context.new_page()
                        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                        await page.wait_for_timeout(3000)
                        for item in await page.evaluate(LINK_FILTER_JS):
                            href = item["href"]
                            if "viewfile.php" in href and "Monthly" in href:
                                raw.append((href, item["text"]))
                    finally:
                        await browser.close()
            except Exception as e:
                logger.debug(f"NJ Playwright scrape failed: {e}")

        links: list[PDFLink] = []
        seen = set()
        for href, text in raw:
            if href in seen:
                continue
            seen.add(href)
            month, year = None, None
            detected = extract_month_year(f"{text} {href}")
            if detected:
                month, year = detected
            # filename from the ?file= query param when present
            if "file=" in href:
                filename = unquote(href.split("file=")[1].split("&")[0])
            else:
                filename = href.rstrip("/").split("/")[-1]
            links.append(PDFLink(
                url=href,
                filename=filename,
                month=month,
                year=year,
                scheme_name=text[:100],
            ))
        return links

    @staticmethod
    def _resolve(href: str, base: str) -> str:
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("http"):
            return href
        return urljoin(base, href)
