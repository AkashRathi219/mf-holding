from __future__ import annotations

from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.utils import get_random_headers

from .base import AMCAdapter, DOCUMENT_EXTENSIONS, PDFLink, extract_month_year


class GenericAdapter(AMCAdapter):
    """Adapter that scrapes server-rendered AMC websites over plain HTTP."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        results: list[PDFLink] = []

        for url in [portfolio_url, factsheet_url]:
            if not url:
                continue
            url = self.clean_url(url)
            try:
                found = await self._scrape_page(url)
                results.extend(found)
            except Exception:
                pass

        return self._dedupe(results)

    async def _scrape_page(self, url: str) -> list[PDFLink]:
        results: list[PDFLink] = []

        async with httpx.AsyncClient(
            headers=get_random_headers(),
            timeout=30,
            follow_redirects=True,
            verify=False,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            text = a_tag.get_text(strip=True)

            if not href.lower().endswith(DOCUMENT_EXTENSIONS):
                continue

            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
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

    @staticmethod
    def _dedupe(links: list[PDFLink]) -> list[PDFLink]:
        seen = set()
        unique = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                unique.append(link)
        return unique
