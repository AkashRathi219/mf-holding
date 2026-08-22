from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from .base import AMCAdapter, PDFLink

logger = logging.getLogger(__name__)

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")

# The registry points at the top-level disclosures index; the monthly portfolio
# documents live under this section.
MONTHLY_URL = "https://www.jioblackrockamc.com/statutory-disclosure/disclosures/monthly-portfolio-disclosure"


class JioBlackRockAdapter(AMCAdapter):
    """Jio BlackRock renders the monthly portfolio behind Ant-Design month/fiscal-
    year dropdowns. We drive them with Playwright and read the resulting links."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []

        url = MONTHLY_URL

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
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception as e:
                    logger.debug(f"Jio goto failed: {e}")
                await page.wait_for_timeout(3000)

                # Locate the month dropdown (the Ant-Select currently showing a month name).
                month_select = None
                for el in await page.locator(".ant-select").all():
                    try:
                        txt = await el.text_content()
                    except Exception:
                        txt = ""
                    if txt and any(m in txt for m in MONTHS):
                        month_select = el
                        break

                if month_select is None:
                    return []

                # Step through each month; the page reloads docs per month.
                for mname in MONTHS:
                    try:
                        await month_select.locator(".ant-select-selector").click()
                        await page.wait_for_timeout(700)
                        opt = page.locator(
                            f".ant-select-item-option:has-text('{mname}')"
                        ).first
                        if await opt.count() == 0:
                            continue
                        await opt.click()
                        await page.wait_for_timeout(2000)
                    except Exception:
                        continue

                    anchors = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('a')).map(a => ({
                            href: a.href || '', text: (a.textContent || '').trim().substring(0, 120)
                        })).filter(l => l.href && (
                            l.href.includes('.pdf') || l.href.includes('.xlsx') ||
                            l.href.includes('.csv') || l.href.includes('.zip')
                        ))
                    """)
                    for a in anchors:
                        href = a["href"]
                        if href.startswith("/"):
                            href = urljoin(url, href)
                        filename = href.rstrip("/").split("/")[-1].split("?")[0]
                        links.append(PDFLink(
                            url=href,
                            filename=filename,
                            month=None,
                            year=None,
                            scheme_name=a["text"][:120],
                        ))
            finally:
                await browser.close()

        # Parse date (DD-MM-YYYY) out of the link text where present.
        seen = set()
        out: list[PDFLink] = []
        for link in links:
            m = DATE_RE.search(link.scheme_name or "")
            if m:
                try:
                    link.month = int(m.group(2))
                    link.year = int(m.group(3))
                except ValueError:
                    pass
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
