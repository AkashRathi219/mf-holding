from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.utils import get_random_headers

logger = logging.getLogger(__name__)

# First bytes of an HTML document (error page / login redirect etc.)
_HTML_SIGNATURES = (b"<!doctype", b"<html", b"<head")


class DocumentDownloader:
    def __init__(
        self,
        output_dir: Path,
        delay_between_requests: float = 2.0,
        max_retries: int = 3,
        timeout: float = 120.0,
        verify_ssl: bool = False,
    ):
        self.output_dir = output_dir
        self.delay = delay_between_requests
        self.max_retries = max_retries
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def get_output_path(
        self, amc_name: str, year: int, month: int, filename: str
    ) -> Path:
        safe_amc = amc_name.replace(" ", "_").replace("/", "-")
        month_str = f"{month:02d}"
        dir_path = self.output_dir / safe_amc / str(year) / month_str
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path / filename

    async def download_file(
        self, url: str, amc_name: str, year: int, month: int, filename: str
    ) -> Path | None:
        output_path = self.get_output_path(amc_name, year, month, filename)

        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info(f"Already downloaded: {output_path.name}")
            return output_path

        try:
            content = await self._fetch(url)
            if content:
                output_path.write_bytes(content)
                logger.info(f"Downloaded: {output_path.name} ({len(content)} bytes)")
                return output_path
        except Exception as e:
            logger.error(f"Failed to download {url}: {e}")

        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch(self, url: str) -> bytes | None:
        content = await self._fetch_httpx(url)
        if content is None and (
            "www.edelweissmf.com" in url
            or "files.hdfcfund.com" in url
            or "cms.hdfcfund.com" in url
        ):
            # Akamai on these hosts blocks plain httpx (403); Chrome TLS
            # impersonation (curl_cffi) passes.
            content = await asyncio.to_thread(self._fetch_curl_cffi, url)
        if content is None:
            return None
        return self._validate_content(content, url)

    async def _fetch_httpx(self, url: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(
                headers=get_random_headers(),
                timeout=self.timeout,
                follow_redirects=True,
                verify=self.verify_ssl,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content or None
        except Exception as e:
            logger.debug(f"httpx fetch failed ({url}): {e}")
            return None

    def _fetch_curl_cffi(self, url: str) -> bytes | None:
        try:
            from curl_cffi import requests as cr
        except ImportError:
            return None
        try:
            resp = cr.get(url, impersonate="chrome", timeout=self.timeout)
            resp.raise_for_status()
            return resp.content or None
        except Exception as e:
            logger.debug(f"curl_cffi fetch failed ({url}): {e}")
            return None

    def _validate_content(self, content: bytes, url: str) -> bytes | None:
        if not content:
            return None
        head = content[:512].lstrip().lower()
        # HTML factsheet pages (e.g. Kotak's kotak.bank.in scheme pages) are
        # intentionally saved as documents; only reject HTML when the target
        # URL was expected to be a binary document (error/redirect page).
        if head.startswith(_HTML_SIGNATURES):
            if url.lower().endswith(".html"):
                return content
            logger.warning(
                f"Response looks like HTML (likely a block/redirect page), "
                f"refusing to save as document: {url}"
            )
            return None
        content_type = ""
        is_binary = (
            content[:5] == b"%PDF-"
            or content[:4] == b"PK\x03\x04"  # ZIP archive (per-scheme bundle)
            or "pdf" in content_type
            or "spreadsheet" in content_type
            or "excel" in content_type
            or "octet-stream" in content_type
            or url.lower().endswith((".pdf", ".xlsx", ".xls", ".csv", ".zip"))
        )

        if is_binary or len(content) > 1000:
            return content

        logger.warning(f"Unexpected content-type: {content_type}, size: {len(content)}")
        return None

    async def download_all(
        self, document_links: list, amc_name: str, year: int, month: int
    ) -> list[Path]:
        downloaded = []
        for i, link in enumerate(document_links):
            if i > 0:
                await asyncio.sleep(self.delay)
            path = await self.download_file(
                url=link.url,
                amc_name=amc_name,
                year=year,
                month=month,
                filename=link.filename,
            )
            if path:
                downloaded.append(path)
        return downloaded
