from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time

from .base import AMCAdapter, PDFLink, MONTH_ABBRS

logger = logging.getLogger(__name__)

API_BASE = "https://api.edelweissmf.com/edelweissmf/api/v1/"
FILE_BASE = "https://www.edelweissmf.com"

# Constants extracted from the production JS bundle (main.*.js).
SECRET = "5b6714126d3149fbab994747b2633287"
HASH_KEY = "r4vcos0ejvndsow95n"
STATIC_IP = "103.0.123.175"

MENUS_URL = "mf/statutory-menus"
SINGLE_URL = "mf/statutory-menus/single"


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey (MD5, 1 iteration) as used by CryptoJS Salted__ output."""
    d = b""
    prev = b""
    while len(d) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        d += prev
    return d[:key_len], d[key_len:key_len + iv_len]


def _decrypt_response(body: str, key_hex: str) -> str:
    """Decrypt an Edelweiss API response body.

    The body is base64 of ``Salted__<8-byte salt><AES-256-CBC ciphertext>``
    where the key/IV come from EVP_BytesToKey(hex-key, salt).
    """
    raw = base64.b64decode(body)
    if raw[:8] != b"Salted__":
        return body
    salt, ct = raw[8:16], raw[16:]
    key, iv = _evp_bytes_to_key(key_hex.encode("ascii"), salt, 32, 16)
    from Crypto.Cipher import AES

    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    pad = plain[-1]
    return plain[:-pad].decode("utf-8", errors="replace")


def _hmac_key(ip: str, timestamp: str) -> str:
    """Session key the API uses to encrypt responses (and requests)."""
    return hmac.new(
        HASH_KEY.encode("ascii"),
        f"{SECRET}{ip}{timestamp}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _month_to_int(month_abbr: str) -> int | None:
    if not month_abbr:
        return None
    try:
        return MONTH_ABBRS.index(month_abbr.strip().lower()[:3]) + 1
    except ValueError:
        return None


class EdelweissAdapter(AMCAdapter):
    """Edelweiss MF factsheets & portfolio disclosures via the api.edelweissmf.com
    API. The site sits behind Akamai and every response is AES-encrypted
    (OpenSSL ``Salted__`` format). All discovery requests need curl_cffi Chrome
    impersonation; file downloads are also blocked for plain httpx (403), so the
    downloader falls back to curl_cffi (see pdf_downloader.DocumentDownloader).
    """

    def __init__(self) -> None:
        self._ip = STATIC_IP

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        links: list[PDFLink] = []

        try:
            from curl_cffi import requests as cr
        except ImportError:
            logger.warning("curl_cffi not installed; Edelweiss adapter disabled")
            return []

        def get_encrypted(path: str, params: dict) -> list[dict] | None:
            """GET an encrypted endpoint, returning the decrypted JSON object."""
            ts = str(int(time.time() * 1000))
            key_hex = _hmac_key(self._ip, ts)
            try:
                resp = cr.get(
                    API_BASE + path,
                    params=params,
                    impersonate="chrome",
                    timeout=30,
                    headers={
                        "x-timestamp": ts,
                        "x-ip-address": self._ip,
                        "Origin": "https://www.edelweissmf.com",
                        "Referer": "https://www.edelweissmf.com/statutory",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.debug(f"Edelweiss {path} ({params}) failed: {e}")
                return None
            if not isinstance(data, dict) or "body" not in data:
                return None
            try:
                decrypted = _decrypt_response(data["body"], key_hex)
                return json_loads(decrypted)
            except Exception as e:
                logger.debug(f"Edelweiss {path} decrypt failed: {e}")
                return None

        def to_link(file_rec: dict) -> PDFLink | None:
            # filePath is the canonical field; downloadFile is sometimes stale
            # (contains a duplicated "/Files/MF/" prefix).
            file_path = file_rec.get("filePath") or file_rec.get("downloadFile") or ""
            if not file_path:
                return None
            file_path = file_path.replace("//", "/")
            url = FILE_BASE + ("/" if not file_path.startswith("/") else "") + file_path
            filename = (
                file_rec.get("systemFileName")
                or url.rstrip("/").split("/")[-1].split("?")[0]
            )
            month = _month_to_int(file_rec.get("month", "") or "")
            year_s = file_rec.get("year", "") or ""
            year = int(year_s) if year_s.isdigit() else None
            return PDFLink(
                url=url,
                filename=filename,
                month=month,
                year=year,
                scheme_name=file_rec.get("fileTitle") or "",
            )

        # --- Monthly portfolio disclosure: statutory > "Portfolio of scheme(s)" ---
        try:
            statutory_menus = await asyncio.to_thread(
                get_encrypted,
                MENUS_URL,
                {"type": "statutory", "fundType": "MF"},
            )
            portfolio_menu = None
            for menu in statutory_menus or []:
                if "Portfolio of scheme" in (menu.get("menuName", "") or ""):
                    portfolio_menu = menu
                    break
            if portfolio_menu:
                data = await asyncio.to_thread(
                    get_encrypted,
                    SINGLE_URL,
                    {
                        "type": "statutory",
                        "fundType": "MF",
                        "menuName": portfolio_menu["menuName"],
                    },
                )
                for f in (data or {}).get("files", []) or []:
                    # Keep the monthly portfolio holdings; drop weekly/fortnightly noise.
                    if (f.get("subMenuName", "") or "").find("Monthly Portfolio") != 0:
                        continue
                    link = to_link(f)
                    if link:
                        links.append(link)
        except Exception as e:
            logger.debug(f"Edelweiss portfolio discovery failed: {e}")

        # --- Factsheets: downloads menu > "FACTSHEETS" ---
        try:
            download_menus = await asyncio.to_thread(
                get_encrypted,
                MENUS_URL,
                {"type": "downloads", "fundType": "MF"},
            )
            factsheet_menu = None
            for menu in download_menus or []:
                if (menu.get("menuName", "") or "").upper() == "FACTSHEETS":
                    factsheet_menu = menu
                    break
            if factsheet_menu:
                data = await asyncio.to_thread(
                    get_encrypted,
                    SINGLE_URL,
                    {
                        "type": "downloads",
                        "fundType": "MF",
                        "menuName": factsheet_menu["menuName"],
                    },
                )
                for f in (data or {}).get("files", []) or []:
                    link = to_link(f)
                    if link:
                        links.append(link)
        except Exception as e:
            logger.debug(f"Edelweiss factsheet discovery failed: {e}")

        seen = set()
        out = []
        for link in links:
            if link.url not in seen:
                seen.add(link.url)
                out.append(link)
        return out


def json_loads(text: str) -> list[dict] | dict | None:
    import json

    try:
        return json.loads(text)
    except Exception:
        return None
