from __future__ import annotations

import asyncio
import logging

import httpx

from .base import AMCAdapter, PDFLink

logger = logging.getLogger(__name__)

FUNDS_URL = "https://www.utimf.com/api/get_investor_scheme_fund"
PORTFOLIO_URL = "https://www.utimf.com/api/get-scheme-portfolio-disclosure"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
    "Referer": "https://www.utimf.com/downloads/scheme-wise-portfolio-disclosure",
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

CONCURRENCY = 6


class UTIAdapter(AMCAdapter):
    """UTI serves scheme-wise portfolio disclosures via a simple GET API
    (dofa_scheme_code + year + month). The fund list comes from
    /api/get_investor_scheme_fund; each fund's code resolves to a portfolio
    file (one per scheme/sector group)."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        async with httpx.AsyncClient(verify=False, timeout=30, headers=HEADERS) as client:
            try:
                resp = await client.get(FUNDS_URL)
                resp.raise_for_status()
                funds = resp.json().get("data", []) or []
            except Exception as e:
                logger.error(f"UTI fund list failed: {e}")
                return []

        funds = [
            {"name": f.get("field_fund_name", ""), "code": f.get("field_dofa_schcode", "")}
            for f in funds if f.get("field_dofa_schcode")
        ]
        logger.info(f"UTI: {len(funds)} funds")

        # Query each fund for the trailing 12 months (covers the target month +
        # recent fallback months).
        import datetime
        today = datetime.date.today()
        months = []
        for i in range(12):
            y = today.year - (1 if today.month - i <= 0 else 0)
            m = (today.month - i - 1) % 12 + 1
            months.append((y, m))

        links: list[PDFLink] = []
        sem = asyncio.Semaphore(CONCURRENCY)

        async def query_fund(fund: dict):
            async with sem:
                async with httpx.AsyncClient(verify=False, timeout=30, headers=HEADERS) as client:
                    for year, month in months:
                        try:
                            r = await client.get(
                                PORTFOLIO_URL,
                                params={"dofa_scheme_code": fund["code"],
                                        "year": str(year),
                                        "month": MONTHS[month - 1]},
                            )
                            if r.status_code != 200:
                                continue
                            rows = r.json().get("rows", []) or []
                        except Exception as e:
                            logger.debug(f"UTI {fund['code']} {year}-{month}: {e}")
                            continue
                        for row in rows:
                            url = row.get("url") or row.get("doc") or ""
                            name = row.get("name", "") or ""
                            if not url:
                                continue
                            filename = url.rstrip("/").split("/")[-1].split("?")[0]
                            links.append(PDFLink(
                                url=url,
                                filename=filename,
                                month=month,
                                year=year,
                                scheme_name=name or fund["name"],
                            ))

        await asyncio.gather(*(query_fund(f) for f in funds))

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        logger.info(f"UTI: {len(out)} portfolio documents discovered")
        return out
