from __future__ import annotations

import datetime
import logging
from urllib.parse import urljoin

from .base import AMCAdapter, PDFLink

logger = logging.getLogger(__name__)

BASE_URL = "https://mf.whiteoakamc.com/resources/downloads/factsheet"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"

EXTRACT_JS = """
    () => Array.from(document.querySelectorAll('a')).map(a => ({
        href: a.href || '', text: (a.textContent || '').trim().substring(0, 80)
    })).filter(l => l.href && /content\\.whiteoakamc\\.com/.test(l.href) &&
        /Factsheet|factsheet/.test(l.text + l.href))
"""


class WhiteOakAdapter(AMCAdapter):
    """WhiteOak publishes one consolidated monthly factsheet PDF per month at
    resources/downloads/factsheet?month=...&year=... The file link (with a
    content hash) only appears on the rendered page, so we load each month."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []

        # Last 24 months covers the target month + recent fallback months.
        today = datetime.date.today()
        pairs = []
        for i in range(24):
            y = today.year - (1 if today.month - i <= 0 else 0)
            m = (today.month - i - 1) % 12 + 1
            pairs.append((y, m))

        links: list[PDFLink] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=UA)
                page = await context.new_page()
                for year, month in pairs:
                    mname = MONTHS[month - 1]
                    url = f"{BASE_URL}?month={mname}&year={year}"
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                        await page.wait_for_timeout(2500)
                    except Exception as e:
                        logger.debug(f"WhiteOak {year}-{mname}: {e}")
                        continue
                    try:
                        items = await page.evaluate(EXTRACT_JS)
                    except Exception:
                        continue
                    for item in items:
                        href = item["href"]
                        if not href.startswith("http"):
                            href = urljoin(url, href)
                        filename = href.rstrip("/").split("/")[-1].split("?")[0]
                        links.append(PDFLink(
                            url=href,
                            filename=filename,
                            month=month,
                            year=year,
                            scheme_name=item["text"] or filename,
                        ))
            finally:
                await browser.close()

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        logger.info(f"WhiteOak: {len(out)} factsheets")
        return out
