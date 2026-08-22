from __future__ import annotations

import logging

import httpx

from .base import AMCAdapter, PDFLink

logger = logging.getLogger(__name__)

API_URL = "https://choicemf.com/api/monthly-portfolio-report/portfolio-website-list"
DOC_BASE = "https://doc.choicemf.com"


class ChoiceAdapter(AMCAdapter):
    """Choice Mutual Fund exposes a monthly-portfolio-report API; the actual
    files are served from doc.choicemf.com."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
                    "Referer": "https://choicemf.com/disclosures/monthly-portfolio",
                    "Content-Type": "application/json",
                },
            ) as client:
                resp = await client.post(API_URL, json={})
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error(f"Choice API failed: {e}")
            return []

        schemes = (data.get("body", {}).get("data")) or []
        links: list[PDFLink] = []

        for scheme in schemes:
            scheme_name = scheme.get("scheme_name", "")
            for report in scheme.get("reports", []) or []:
                file_path = report.get("file_path", "")
                report_date = report.get("report_date", "")
                if not file_path:
                    continue

                month, year = None, None
                try:
                    parts = report_date.split("-")
                    year = int(parts[0])
                    month = int(parts[1])
                except (IndexError, ValueError):
                    pass

                rel = file_path.lstrip("/")
                url = f"{DOC_BASE}/{rel}"
                filename = rel.split("/")[-1].split("?")[0]
                links.append(PDFLink(
                    url=url,
                    filename=filename,
                    month=month,
                    year=year,
                    scheme_name=scheme_name,
                ))

        return links
