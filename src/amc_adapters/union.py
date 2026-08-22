from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from .base import AMCAdapter, PDFLink, extract_month_year

logger = logging.getLogger(__name__)

MONTHLY_URL = "https://www.unionmf.com/about-us/downloads/monthly-portfolio"

DATE_RE = re.compile(r"-(\d{2})-(\d{2})-(\d{4})\.")


class UnionAdapter(AMCAdapter):
    """Union MF renders the monthly portfolio behind a #yearFilter dropdown; the
    document list is produced server-side on selection."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []

        links: list[PDFLink] = []

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
                try:
                    await page.goto(MONTHLY_URL, wait_until="domcontentloaded", timeout=40000)
                except Exception as e:
                    logger.debug(f"Union goto failed: {e}")
                await page.wait_for_timeout(4000)

                year_sel = page.locator("#yearFilter")
                if await year_sel.count() == 0:
                    return []

                # Step through years to collect documents for all available months.
                years = []
                for opt in await year_sel.locator("option").all():
                    try:
                        t = (await opt.text_content()).strip()
                    except Exception:
                        t = ""
                    if t.isdigit():
                        years.append(t)
                years = sorted(years, reverse=True)

                for year in years[:8]:
                    try:
                        await year_sel.select_option(label=year)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        continue
                    anchors = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('a')).map(a => ({
                            href: a.href || '', text: (a.textContent || '').trim().substring(0, 80)
                        })).filter(l => l.href && (
                            l.href.includes('fund-portfolio') &&
                            (l.href.includes('.pdf') || l.href.includes('.xlsx') ||
                             l.href.includes('.csv') || l.href.includes('.xls'))
                        ))
                    """)
                    for a in anchors:
                        href = a["href"]
                        if href.startswith("/"):
                            href = urljoin(MONTHLY_URL, href)
                        month, year_v = None, None
                        detected = extract_month_year(href)
                        if detected:
                            month, year_v = detected
                        if month is None:
                            m = DATE_RE.search(href)
                            if m:
                                try:
                                    month = int(m.group(2))
                                    year_v = int(m.group(3))
                                except ValueError:
                                    pass
                        filename = href.rstrip("/").split("/")[-1].split("?")[0]
                        links.append(PDFLink(
                            url=href,
                            filename=filename,
                            month=month,
                            year=year_v,
                            scheme_name=a["text"][:80],
                        ))
            finally:
                await browser.close()

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
