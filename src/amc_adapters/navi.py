from __future__ import annotations

import logging

from .base import AMCAdapter, PDFLink, extract_month_year
from .playwright_adapter import LINK_FILTER_JS

logger = logging.getLogger(__name__)

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

SET_FY_JS = """
    (fy) => {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
        let changed = false;
        for (const s of Array.from(document.querySelectorAll('select'))) {
            const vals = Array.from(s.options).map(o => o.value);
            if (vals.includes(fy)) {
                setter.call(s, fy);
                s.dispatchEvent(new Event('change', { bubbles: true }));
                changed = true;
            }
        }
        return changed;
    }
"""

SET_MONTH_JS = """
    (mname) => {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
        let changed = false;
        for (const s of Array.from(document.querySelectorAll('select'))) {
            const idx = Array.from(s.options).findIndex(o => (o.text || '').trim() === mname);
            if (idx >= 0) {
                setter.call(s, s.options[idx].value);
                s.dispatchEvent(new Event('change', { bubbles: true }));
                changed = true;
            }
        }
        return changed;
    }
"""


class NaviAdapter(AMCAdapter):
    """Navi publishes scheme-wise monthly portfolios behind a financial-year +
    month selector. We drive the (hidden) selects with JS, step through every
    month, and collect the resulting document links."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []

        url = self.clean_url(portfolio_url or factsheet_url)
        if not url:
            return []

        raw_items: list[tuple[str, str]] = []

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
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                except Exception as e:
                    logger.debug(f"Navi goto failed: {e}")
                await page.wait_for_timeout(4000)

                for fy in (f"{2025}-{2026}", f"{2026}-{2027}"):
                    try:
                        await page.evaluate(SET_FY_JS, fy)
                    except Exception:
                        continue
                    await page.wait_for_timeout(1500)
                    for mname in MONTHS:
                        try:
                            await page.evaluate(SET_MONTH_JS, mname)
                        except Exception:
                            continue
                        await page.wait_for_timeout(1200)
                        try:
                            items = await page.evaluate(LINK_FILTER_JS)
                            raw_items.extend((it["href"], it["text"]) for it in items)
                        except Exception:
                            continue
            finally:
                await browser.close()

        links: list[PDFLink] = []
        for href, text in raw_items:
            if not href.startswith("http"):
                continue
            month, year = None, None
            detected = extract_month_year(f"{text} {href}")
            if detected:
                month, year = detected
            filename = href.rstrip("/").split("/")[-1].split("?")[0]
            links.append(PDFLink(
                url=href,
                filename=filename,
                month=month,
                year=year,
                scheme_name=text[:100],
            ))

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
