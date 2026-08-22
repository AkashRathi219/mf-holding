from __future__ import annotations

import base64
import logging
import re

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .base import AMCAdapter, PDFLink, MONTH_NAMES, MONTH_ABBRS

logger = logging.getLogger(__name__)

API_BASE = "https://jmmfapi.jmfinancialmf.com/api/"
FILE_BASE = "https://www.jmfinancialmf.com/"

# AES-256-CBC key/IV from the frontend bundle (REACT_APP_AES_KEY/IV).
AES_KEY = b"6fa979f20126cb08aa645a8f495f6d85"
AES_IV = b"I8zyA4lVhMCaJ5Kg"

# (categoryId, subcategoryId) discovered via GetDownloadDrop/GetDownloadNewSub.
FACTSHEET_CAT, FACTSHEET_SUB = 10, 37        # "Factsheet" monthly PDFs
PORTFOLIO_CAT, PORTFOLIO_SUB = 2, 3          # "Fortnightly Portfolio of Schemes"


def _decrypt(payload: str) -> str:
    """Decrypt an API response (AES-256-CBC, PKCS7, Latin1 output)."""
    ct = base64.b64decode(payload)
    plain = unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(ct), 16)
    return plain.decode("latin-1", errors="replace")


def _title_month_year(title: str) -> tuple[int | None, int | None]:
    """Parse "Factsheet July 2026" / "Fortnightly Portfolio- JM ... July 31, 2026"."""
    year = None
    m = re.search(r"(\d{4})", title)
    if m:
        year = int(m.group(1))
    month = None
    for i, name in enumerate(MONTH_NAMES, 1):
        if name.lower() in title.lower():
            month = i
            break
    if month is None:
        for i, abbr in enumerate(MONTH_ABBRS, 1):
            if abbr.lower() in title.lower():
                month = i
                break
    return month, year


class JMFinancialAdapter(AMCAdapter):
    """JM Financial Mutual Fund downloads via the jmmfapi API.

    Endpoints: ``GetDownloadNewSub`` lists subcategories; ``GetDownloadNew``
    lists files. Every response payload is AES-256-CBC encrypted (key/IV from
    the JS bundle) and base64; output is decoded as Latin1. File URLs are
    ``www.jmfinancialmf.com/CMS/...`` and download with plain httpx.
    """

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            verify=False, timeout=30, headers=headers, follow_redirects=True
        ) as client:

            async def query(endpoint: str, body: dict) -> list[dict] | None:
                try:
                    resp = await client.post(API_BASE + endpoint, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                    if not isinstance(data, dict) or "data" not in data:
                        return None
                    return __import__("json").loads(_decrypt(data["data"]))
                except Exception as e:
                    logger.debug(f"JM {endpoint} ({body}) failed: {e}")
                    return None

            links: list[PDFLink] = []

            # --- Factsheets (monthly PDFs) ---
            factsheets = await query(
                "GetDownloadNew",
                {"IICategoryID": FACTSHEET_CAT, "IISubCategoryID": FACTSHEET_SUB, "IVsearch": ""},
            )
            for f in factsheets or []:
                path = f.get("FileName") or ""
                if not path:
                    continue
                title = f.get("Title") or ""
                month, year = _title_month_year(title)
                links.append(PDFLink(
                    url=FILE_BASE + path.lstrip("/"),
                    filename=path.rstrip("/").split("/")[-1],
                    month=month,
                    year=year,
                    scheme_name=title,
                    document_type="factsheet",
                ))

            # --- Fortnightly Portfolio of Schemes (XLSX) ---
            portfolios = await query(
                "GetDownloadNew",
                {"IICategoryID": PORTFOLIO_CAT, "IISubCategoryID": PORTFOLIO_SUB, "IVsearch": ""},
            )
            for f in portfolios or []:
                path = f.get("FileName") or ""
                if not path:
                    continue
                title = f.get("Title") or ""
                month, year = _title_month_year(title)
                links.append(PDFLink(
                    url=FILE_BASE + path.lstrip("/"),
                    filename=path.rstrip("/").split("/")[-1],
                    month=month,
                    year=year,
                    scheme_name=title,
                    document_type="monthly_portfolio",
                ))

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
