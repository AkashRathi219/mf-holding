from __future__ import annotations

import logging
from urllib.parse import urljoin

from .base import AMCAdapter, PDFLink, extract_month_year

logger = logging.getLogger(__name__)

MONTHLY_URL = "https://taurusmutualfund.com/monthly-portfolio"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


class TaurusAdapter(AMCAdapter):
    """Taurus uses Drupal exposed filters (year + month) whose AJAX view re-renders
    the scheme-wise monthly portfolio reports on every selection."""

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
                    await page.goto(MONTHLY_URL, wait_until="domcontentloaded", timeout=25000)
                except Exception as e:
                    logger.debug(f"Taurus goto failed: {e}")
                await page.wait_for_timeout(4000)

                # Collect available years from the year select.
                selects = await page.query_selector_all("select")
                if len(selects) < 2:
                    return []
                years = []
                for opt in await selects[0].query_selector_all("option"):
                    try:
                        t = (await opt.text_content()).strip()
                    except Exception:
                        t = ""
                    if t.isdigit():
                        years.append(t)
                years = sorted(years, reverse=True)[:3]

                for year in years:
                    try:
                        year_sel = await page.query_selector("select")
                        await year_sel.select_option(label=year)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        continue
                    for mname in MONTHS:
                        try:
                            month_sel = (await page.query_selector_all("select"))[-1]
                            await month_sel.select_option(label=mname)
                            await page.wait_for_timeout(1800)
                        except Exception:
                            continue
                        anchors = await page.evaluate("""
                            () => Array.from(document.querySelectorAll('a')).map(a => ({
                                href: a.href || '', text: (a.textContent || '').trim().substring(0, 80)
                            })).filter(l => l.href && l.href.includes('Monthly_Portfolio_Report'))
                        """)
                        for a in anchors:
                            href = a["href"]
                            if href.startswith("/"):
                                href = urljoin(MONTHLY_URL, href)
                            month, year_v = None, None
                            detected = extract_month_year(f"{a['text']} {href}")
                            if detected:
                                month, year_v = detected
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
