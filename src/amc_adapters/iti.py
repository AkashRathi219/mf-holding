from __future__ import annotations

import base64
import json
import logging
import random
import string

import httpx

from .base import AMCAdapter, PDFLink, extract_month_year

logger = logging.getLogger(__name__)

API_URL = "https://itiamc.com/jeeth/api/v1/catalog/getPartnerDocumentByType"

# AES-128-CBC key/IV discovered in ITI's front-end bundle.
KEY = b"aar6tzij8o1snaar"
IV = b"0123456789ABCDEF"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
    "Content-Type": "application/json",
    "Origin": "https://www.itiamc.com",
    "Referer": "https://www.itiamc.com/statuory-disclosure",
}


def _decrypt_e_data(e_data: str) -> dict:
    from Crypto.Cipher import AES

    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    raw = cipher.decrypt(base64.b64decode(e_data))
    return json.loads(raw[:-raw[-1]].decode("utf-8", errors="replace"))


def _encrypt_payload(obj: dict) -> str:
    from Crypto.Cipher import AES

    data = json.dumps(obj, separators=(",", ":")).encode()
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    return base64.b64encode(cipher.encrypt(data)).decode()


class ITIAdapter(AMCAdapter):
    """ITI wraps its jeeth/catalog API in AES-128-CBC (eData). We replicate the
    encryption, call getPartnerDocumentByType directly, and decrypt the response
    to obtain the statutory / portfolio document URLs."""

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        guid = "".join(random.choices(string.ascii_letters + string.digits, k=32))
        try:
            async with httpx.AsyncClient(verify=False, timeout=30, headers=HEADERS) as client:
                resp = await client.post(API_URL, json={"eData": _encrypt_payload({"type": "Disclosure", "guid": guid})})
                resp.raise_for_status()
                data = _decrypt_e_data(resp.json()["eData"])
        except Exception as e:
            logger.error(f"ITI API failed: {e}")
            return []

        links: list[PDFLink] = []
        for group in data.get("data", {}).get("typeList", []) or []:
            for sub in group.get("subTypesList", []) or []:
                for topic in sub.get("topicsList", []) or []:
                    name = topic.get("fileName", "") or ""
                    url = topic.get("url", "") or ""
                    if not url or not any(k in name.lower() for k in
                                          ["portfolio", "holding", "investment", "statement"]):
                        continue
                    month, year = None, None
                    detected = extract_month_year(f"{name} {url}")
                    if detected:
                        month, year = detected
                    filename = url.rstrip("/").split("/")[-1].split("?")[0]
                    links.append(PDFLink(
                        url=url,
                        filename=filename,
                        month=month,
                        year=year,
                        scheme_name=name[:120],
                    ))

        return links
