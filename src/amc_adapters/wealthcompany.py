from __future__ import annotations

import logging
import re

import httpx

from .base import AMCAdapter, PDFLink, MONTH_NAMES, MONTH_ABBRS

logger = logging.getLogger(__name__)

MONTHLY_URL = "https://www.wealthcompanyamc.in/literature-forms/portfolio-documents/monthly/"
FACTSHEET_URL = "https://www.wealthcompanyamc.in/literature-forms/scheme-documents/factsheets/"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"


def _clean_js(s: str) -> str:
    """Unescape the JSON embedded in the Next.js page."""
    return s.replace("\\\\", "\\").replace('\\"', '"').replace("\\u0026", "&")


def _month_year_from_doc(doc: dict) -> tuple[int | None, int | None]:
    """Extract month/year from the document name (e.g. "... - July 31, 2026")."""
    text = doc.get("name", "")
    year = None
    m = re.search(r"(20\d{2})", text)
    if m:
        year = int(m.group(1))
    month = None
    for i, name in enumerate(MONTH_NAMES, 1):
        if name.lower() in text.lower():
            month = i
            break
    if month is None:
        for i, abbr in enumerate(MONTH_ABBRS, 1):
            if abbr.lower() in text.lower():
                month = i
                break
    # Fall back to ISO date YYYY-MM-DD
    if month is None:
        m = re.search(r"(\d{4})-(\d{2})-\d{2}", text)
        if m:
            month = int(m.group(2))
            year = int(m.group(1))
    return month, year


class WealthCompanyAdapter(AMCAdapter):
    """The Wealth Company AMC (wealthcompanyamc.in).

    The Next.js pages embed the full document catalogue (name + /uploads URL)
    in escaped JSON inside the HTML. Monthly and fortnightly portfolio
    disclosures are XLSX with ISIN-level holdings; factsheets are PDFs.
    """

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        headers = {"User-Agent": UA}
        links: list[PDFLink] = []

        async with httpx.AsyncClient(
            verify=False, timeout=30, headers=headers, follow_redirects=True
        ) as client:
            for base, kind in [
                (MONTHLY_URL, "monthly_portfolio"),
                (FACTSHEET_URL, "factsheet"),
            ]:
                try:
                    resp = await client.get(base)
                    resp.raise_for_status()
                except Exception as e:
                    logger.debug(f"WealthCompany {base} failed: {e}")
                    continue

                clean = _clean_js(resp.text)
                # Each record: "name":"...",...,"attachment":{...,"url":"/uploads/...xlsx"}
                records = re.findall(
                    r'"name":"([^"]*?)".{0,500}?"url":"(/uploads/[^"]+?\.(?:xlsx|xls|pdf))"',
                    clean,
                    re.S,
                )
                for name, url in records:
                    if not name or not url:
                        continue
                    month, year = _month_year_from_doc({"name": name})
                    links.append(PDFLink(
                        url="https://www.wealthcompanyamc.in" + url,
                        filename=url.rstrip("/").split("/")[-1],
                        month=month,
                        year=year,
                        scheme_name=name,
                        document_type=kind,
                    ))

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
