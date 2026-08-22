from __future__ import annotations

import logging
import re

import httpx

from .base import AMCAdapter, PDFLink

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.axismf.com/downloads/products"
API_URL = "https://www.axismf.com/cms/product/factsheet"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"


class AxisAdapter(AMCAdapter):
    """Axis factsheets live behind a Next.js portal whose /cms/product/factsheet
    API requires a Bearer token issued to the browser session. We obtain the
    token with one headless page load, then query all months via plain HTTP."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        token = await self._capture_token()
        if not token:
            logger.warning("Axis: could not obtain bearer token")
            return []

        links: list[PDFLink] = []
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Referer": PAGE_URL,
        }

        async with httpx.AsyncClient(verify=False, timeout=30, headers=headers) as client:
            for year in (2026, 2025, 2024):
                for mname in MONTHS:
                    try:
                        resp = await client.post(API_URL, json={"year": str(year), "month": mname})
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                    except Exception as e:
                        logger.debug(f"Axis ({year}-{mname}) failed: {e}")
                        continue
                    for item in (data.get("data", {}).get("productFactSheetData")) or []:
                        url = item.get("documentUrl", "")
                        name = item.get("name", "") or ""
                        if not url:
                            continue
                        month, year_v = None, None
                        m = re.search(r"([A-Za-z]+)[- ](\d{4})", name)
                        if m and m.group(1).capitalize() in MONTHS:
                            month = MONTHS.index(m.group(1).capitalize()) + 1
                            year_v = int(m.group(2))
                        filename = url.rstrip("/").split("/")[-1].split("?")[0]
                        links.append(PDFLink(
                            url=url,
                            filename=filename,
                            month=month,
                            year=year_v,
                            scheme_name=name,
                        ))

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out

    async def _capture_token(self) -> str | None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None

        token: list[str] = []

        def on_request(req):
            if "/cms/product/factsheet" in req.url:
                auth = req.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    token.append(auth[len("Bearer "):])

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=UA)
                page = await context.new_page()
                page.on("request", on_request)
                try:
                    await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.debug(f"Axis token page load: {e}")
                await page.wait_for_timeout(5000)
                try:
                    el = page.get_by_text("Factsheet", exact=True).first
                    if await el.count():
                        await el.click(timeout=2500)
                        await page.wait_for_timeout(2000)
                except Exception:
                    pass
            finally:
                await browser.close()

        return token[0] if token else None
