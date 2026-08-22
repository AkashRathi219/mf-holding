from __future__ import annotations

import abc
import re
from dataclasses import dataclass

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

MONTH_ABBRS = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]

# File extensions we consider valid portfolio documents (PDF or SEBI
# machine-readable). ZIP archives (per-scheme monthly portfolio bundles) are
# extracted and parsed member-by-member by src/zip_parser.py.
DOCUMENT_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".csv", ".zip")


@dataclass
class PDFLink:
    url: str
    filename: str
    month: int | None = None
    year: int | None = None
    scheme_name: str | None = None
    document_type: str = "monthly_portfolio"


def month_year_match(text: str, target_month: int, target_year: int) -> bool:
    """Check whether *text* references the given month and year."""
    text_lower = text.lower()

    for i, name in enumerate(MONTH_NAMES, 1):
        if i == target_month and name in text_lower:
            return str(target_year) in text

    for i, abbr in enumerate(MONTH_ABBRS, 1):
        if i == target_month and abbr in text_lower:
            return str(target_year) in text

    return False


def extract_month_year(text: str) -> tuple[int, int] | None:
    """Best-effort extraction of a (month, year) pair from document text/URLs.

    Returns ``None`` when no month or year can be identified. URL-encoded
    spaces (``%20``) are decoded first so encoded day numbers aren't mistaken
    for years.
    """
    if not text:
        return None

    from urllib.parse import unquote

    text_lower = unquote(text).lower()

    month = None
    for i, name in enumerate(MONTH_NAMES, 1):
        if name in text_lower:
            month = i
            break
    if month is None:
        for i, abbr in enumerate(MONTH_ABBRS, 1):
            if abbr in text_lower:
                month = i
                break
    if month is None:
        return None

    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text_lower)
    if not years:
        return None

    return (month, int(years[0]))


class AMCAdapter(abc.ABC):
    """Base class for AMC-specific portfolio document discovery adapters."""

    async def discover_documents(
        self,
        portfolio_url: str,
        factsheet_url: str,
        target_month: int,
        target_year: int,
    ) -> list[PDFLink]:
        """Discover documents for a specific month/year.

        Default implementation filters :meth:`discover_documents_all` by the
        detected month/year of each link. Links whose month/year could not be
        detected (e.g. current-month-only factsheet pages) are assigned the
        most recent dated link's month/year - or the target month/year when no
        dated link exists - so undated factsheets are still captured.
        """
        all_links = await self.discover_documents_all(portfolio_url, factsheet_url)
        dated = [
            link for link in all_links
            if link.month is not None and link.year is not None
        ]
        undated = [link for link in all_links if link.month is None or link.year is None]

        matched = [link for link in dated if link.month == target_month and link.year == target_year]
        if not matched and dated:
            latest = max((link.year, link.month) for link in dated)
            matched = [link for link in dated if (link.year, link.month) == latest]

        # Assign undated links the target month/year (current-month factsheets).
        for link in undated:
            link.month = target_month
            link.year = target_year
        return matched + undated

    @abc.abstractmethod
    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        """Discover every portfolio document link (any month), with detected
        ``month``/``year`` populated where possible."""
        ...

    def clean_url(self, url: str) -> str:
        """Normalize a URL (fix encoding issues, trailing slashes, etc.)."""
        url = url.replace("\\u0026", "&")
        url = url.replace("\\/", "/")
        if url.startswith("http://"):
            url = "https://" + url[7:]
        return url.rstrip("/")
