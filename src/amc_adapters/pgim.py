from __future__ import annotations

import logging

import httpx

from .base import AMCAdapter, PDFLink, extract_month_year

logger = logging.getLogger(__name__)

API_URL = "https://www.pgimindia.com/api/v1/brochure/published/disclosure"

# "Portfolios > Monthly Portfolio" section on the PGIM disclosures portal.
PGIM_SECTION_ID = "SECTION_747960037"
PGIM_HEADER_ID = 2


class PGIMAdapter(AMCAdapter):
    """Uses the PGIM India brochure/disclosure API to list scheme-wise monthly
    portfolio XLSX files."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        body = {
            "headerId": PGIM_HEADER_ID,
            "sectionId": PGIM_SECTION_ID,
            "source": "W",
            "branchCode": None,
        }

        try:
            async with httpx.AsyncClient(
                verify=False, timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
                    "Content-Type": "application/json",
                },
            ) as client:
                resp = await client.post(API_URL, json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error(f"PGIM API failed: {e}")
            return []

        links: list[PDFLink] = []
        for tab in data.get("data", []) or []:
            for disclosure in tab.get("content", []) or []:
                pdf_path = disclosure.get("pdfPath", "")
                title = disclosure.get("title", "")
                if not pdf_path:
                    continue

                month, year = None, None
                if disclosure.get("month") and disclosure.get("year"):
                    try:
                        m = disclosure["month"].lower()
                        month = extract_month_year(f"1 {m} {disclosure['year']}")[0]
                        year = int(disclosure["year"])
                    except (TypeError, ValueError, IndexError):
                        detected = extract_month_year(f"{title} {pdf_path}")
                        if detected:
                            month, year = detected
                else:
                    detected = extract_month_year(f"{title} {pdf_path}")
                    if detected:
                        month, year = detected

                filename = pdf_path.rstrip("/").split("/")[-1].split("?")[0]
                links.append(PDFLink(
                    url=pdf_path,
                    filename=filename,
                    month=month,
                    year=year,
                    scheme_name=title,
                ))

        return links
