from __future__ import annotations

import logging
from urllib.parse import urljoin

from .base import AMCAdapter, PDFLink, extract_month_year

logger = logging.getLogger(__name__)

LINK_FILTER_JS = """
    () => {
        const results = [];
        document.querySelectorAll('a').forEach(a => {
            const href = a.href || '';
            const text = (a.textContent || '').trim();
            if (href && (
                href.includes('.pdf') || href.includes('.xlsx') ||
                href.includes('.xls') || href.includes('.csv') ||
                text.toLowerCase().includes('download')
            )) {
                results.push({href, text: text.substring(0, 200)});
            }
        });
        return results;
    }
"""


class PlaywrightAdapter(AMCAdapter):
    """Adapter that uses Playwright to render JS-heavy AMC websites."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        results: list[PDFLink] = []

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed, skipping JS-rendered sites")
            return results

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

                for url in [portfolio_url, factsheet_url]:
                    if not url:
                        continue
                    url = self.clean_url(url)
                    try:
                        found = await self._scrape_page(page, url)
                        results.extend(found)
                    except Exception as e:
                        logger.debug(f"Playwright scrape failed for {url}: {e}")
            finally:
                await browser.close()

        seen = set()
        unique = []
        for link in results:
            if link.url not in seen:
                seen.add(link.url)
                unique.append(link)
        return unique

    async def _scrape_page(self, page, url: str) -> list[PDFLink]:
        results: list[PDFLink] = []

        try:
            await page.goto(url, wait_until="load", timeout=25000)
        except Exception:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(3000)

        links_data = await page.evaluate(LINK_FILTER_JS)

        for link_data in links_data:
            href = link_data["href"]
            text = link_data["text"]

            if href.startswith("/"):
                href = urljoin(url, href)

            if not href.startswith("http"):
                continue

            month, year = None, None
            detected = extract_month_year(f"{text} {href}")
            if detected:
                month, year = detected

            filename = href.rstrip("/").split("/")[-1].split("?")[0]
            results.append(PDFLink(
                url=href,
                filename=filename,
                month=month,
                year=year,
            ))

        return results
