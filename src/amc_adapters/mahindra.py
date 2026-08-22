from __future__ import annotations

import asyncio
import base64
import logging
import re

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .base import AMCAdapter, PDFLink, MONTH_NAMES, MONTH_ABBRS

logger = logging.getLogger(__name__)

API_BASE = "https://investorapi.mahindramanulife.com/api/v1/web"
DOWNLOADS_URL = API_BASE + "/preLogin/downloads"

# AES-256-CBC key/IV extracted from the production JS bundle (index-*.js).
AES_KEY = b"mahindra2024mahindra2024mahindra"
AES_IV = b"hasnainsheikh202"

# Category tree names.
FUND_FACTSHEET = "Fund Factsheet"
MONTHLY_PORTFOLIO = "Monthly Portfolio Disclosure"
SCHEME_SUMMARY = "Scheme Summary Documents"

# Categories whose files are NOT portfolio/factsheet documents.
_IRRELEVANT = {
    "notices & addendum", "financials", "mandatory disclosures", "investors",
    "distributors", "sid/sai", "total expense ratio",
}


def _decrypt(payload: str) -> str:
    """Decrypt an investorapi response payload (AES-256-CBC, PKCS7)."""
    ct = base64.b64decode(payload)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    plain = unpad(cipher.decrypt(ct), 16)
    return plain.decode("utf-8", errors="replace")


def _month_to_int(month_abbr: str) -> int | None:
    if not month_abbr:
        return None
    text = month_abbr.strip().lower()
    for i, name in enumerate(MONTH_NAMES, 1):
        if name in text:
            return i
    for i, abbr in enumerate(MONTH_ABBRS, 1):
        if abbr in text:
            return i
    return None


def _parse_title_month_year(title: str) -> tuple[int | None, int | None]:
    """Parse "Monthly Portfolio Disclosure - July, 2026" into (month, year)."""
    m = re.search(r"(\d{4})", title)
    year = int(m.group(1)) if m else None
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


class MahindraAdapter(AMCAdapter):
    """Mahindra Manulife downloads via the encrypted investorapi.

    Every investorapi response body is an AES-256-CBC encrypted ``payload``
    (OpenSSL PKCS7) using a hardcoded key/IV from the frontend bundle. We
    decrypt it and surface the *Monthly Portfolio Disclosure* (one XLSX per
    month with ISIN-level holdings) plus the consolidated *Fund Factsheet*
    PDFs and per-scheme Scheme Summary Documents. File downloads are plain
    httpx from cms.mahindramanulife.com / www.mahindramanulife.com.
    """

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
            "platform": "web",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=60, headers=headers, follow_redirects=True
            ) as client:
                resp = await client.get(DOWNLOADS_URL)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error(f"Mahindra Manulife downloads API failed: {e}")
            return []

        if not isinstance(data, dict) or "payload" not in data:
            logger.warning("Mahindra Manulife: unexpected API response shape")
            return []

        try:
            decrypted = _decrypt(data["payload"])
            import json
            structure = json.loads(decrypted)
        except Exception as e:
            logger.error(f"Mahindra Manulife: decrypt/parse failed: {e}")
            return []

        nodes = structure.get("data", []) if isinstance(structure, dict) else []
        links: list[PDFLink] = []

        def collect(cats: list[dict], context: list[str]) -> None:
            for cat in cats or []:
                name = (cat.get("categoryName") or "").strip()
                ctx = context + [name]
                for f in cat.get("files") or []:
                    url = f.get("fileUrl") or f.get("redirectUrl") or ""
                    if not url:
                        continue
                    title = f.get("title") or name
                    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
                    filename = url.rstrip("/").split("/")[-1].split("?")[0]
                    month, year = _parse_title_month_year(title)
                    links.append(PDFLink(
                        url=url,
                        filename=filename,
                        month=month,
                        year=year,
                        scheme_name=title,
                        document_type="monthly_portfolio" if name == MONTHLY_PORTFOLIO else "factsheet",
                    ))
                collect(cat.get("subcategories") or [], ctx)

        collect(nodes, [])

        # Keep only relevant documents (monthly portfolio disclosures +
        # fund factsheets + scheme summary docs). Drop forms/notices/etc.
        kept: list[PDFLink] = []
        for link in links:
            title = (link.scheme_name or "").lower()
            if any(k in title for k in (
                "portfolio disclosure", "fund factsheet", "factsheet",
                "scheme summary",
            )):
                kept.append(link)

        seen = set()
        out = []
        for link in kept:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out
