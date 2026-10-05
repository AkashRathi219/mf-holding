"""Remediation handlers: taxonomy code -> corrected data (SPEC §6 / PLAN T8 / AC-7, AC-8, AC-10).

AMC holdings data arrives broken in four documented ways, one handler each,
dispatched by the fixed failure code from ``src.agents.taxonomy``:

- ``ERR_SCALE_FRACTION_NAV``    -> :func:`normalize_scale`  (fractions rescaled to percent)
- ``ERR_PDF_HIGH_PAGE_DENSITY`` -> :func:`rank_pdf_pages`   (top-N pages by holdings density)
- ``ERR_WORKBOOK_MULTI_SHEET``  -> :func:`select_sheets`    (top-N sheets by holdings density)
- ``ERR_ENCRYPTED_PAYLOAD_AES`` -> :func:`decrypt_payload`  (OpenSSL ``Salted__`` envelope)

Common contract: every handler returns ``(result, info)`` and never raises for
bad data - an unrecoverable input returns ``(None, {"error": ..., "failure_code":
<its taxonomy code>})`` (AC-5: every failure carries a valid code), while a
success never carries a ``failure_code`` key at all.  Heavy I/O stays out of
this module: callers inject ``page_texts`` / ``sheet_texts`` / ``decryptor``
for deterministic tests, real PDF text extraction (pymupdf) and real workbook
reading (pandas) are lazy-imported, and AES-256-CBC decryption is always
delegated to an injected callable - the repo's real cipher path (Edelweiss
adapter, pycryptodome) supplies it; this module only parses the envelope and
derives the key/IV with the pure :func:`evp_bytes_to_key` (OpenSSL
EVP_BytesToKey, MD5).

Page/sheet scoring mirrors the proven ``scripts/linksheet_sync.py`` block
(``_ISIN_RE`` / ``_PCT_LINE_RE`` / ``_HOLDINGS_KW_RE`` / ``_SHEET_KW_RE`` and
the ``ai_extract_pdf`` scoring loop) - the promotion this module performs per
PLAN §3.  Weight parsing mirrors ``src.agents.tiers`` (same key order and
``"2.35%"``-string tolerance).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from src.agents.taxonomy import FAILURE_METADATA

CODE_SCALE_FRACTION_NAV = "ERR_SCALE_FRACTION_NAV"
CODE_PDF_HIGH_PAGE_DENSITY = "ERR_PDF_HIGH_PAGE_DENSITY"
CODE_WORKBOOK_MULTI_SHEET = "ERR_WORKBOOK_MULTI_SHEET"
CODE_ENCRYPTED_PAYLOAD_AES = "ERR_ENCRYPTED_PAYLOAD_AES"

for _code in (
    CODE_SCALE_FRACTION_NAV,
    CODE_PDF_HIGH_PAGE_DENSITY,
    CODE_WORKBOOK_MULTI_SHEET,
    CODE_ENCRYPTED_PAYLOAD_AES,
):
    if _code not in FAILURE_METADATA:
        raise RuntimeError(f"remediation code {_code} is not in the fixed failure taxonomy")

FRACTION_MIN = 0.90
FRACTION_MAX = 1.10
SCALE_FACTOR = 100.0
SCALE_DECIMALS = 6
MIN_PAGE_CHARS = 40
MAX_PCT_HITS = 20

ISIN_RE = re.compile(r"\bIN[EZ][0-9A-Z]{9}\b")
PCT_LINE_RE = re.compile(r"\d{1,2}\.\d{1,2}\s*%")
HOLDINGS_KW_RE = re.compile(
    r"%\s*to\s*(?:nav|aum)|% of (?:nav|aum)|equity holdings|debt holdings|"
    r"instrument|market value|book value|isin|name of the instrument",
    re.I,
)
SHEET_KW_RE = re.compile(r"isin|portfolio|holding|% to nav|instrument|company", re.I)

SALT_MAGIC = b"Salted__"
SALT_LEN = 8
AES_KEY_LEN = 32
AES_IV_LEN = 16
AES_BLOCK_LEN = 16

_WEIGHT_KEYS: tuple[str, ...] = ("percent_nav", "weight_pct", "pct", "%")


def _failure(code: str, error: str) -> tuple[None, dict[str, object]]:
    return None, {"error": error, "failure_code": code}


def _as_bytes(value: object, what: str) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise TypeError(f"{what} must be str or bytes-like, got {type(value).__name__}")


def _parse_percent(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _row_weight(row: Mapping[str, object]) -> tuple[float, str] | None:
    for key in _WEIGHT_KEYS:
        if key not in row:
            continue
        parsed = _parse_percent(row[key])
        if parsed is not None:
            return parsed, key
    return None


def normalize_scale(
    rows: Sequence[Mapping[str, object]],
    *,
    already_normalized: bool = False,
) -> tuple[list[dict[str, object]] | None, dict[str, object]]:
    """Fix ``ERR_SCALE_FRACTION_NAV``: fraction-scale weights rescaled to percent.

    Weights are read from ``percent_nav`` / ``weight_pct`` / ``pct`` / ``%``
    (same key order as ``src.agents.tiers``; plain floats and ``"2.35%"``-style
    strings both parse).  When ``0.90 <= sum <= 1.10`` the values are FRACTIONS
    and every weight is multiplied by 100 - written back into the key it came
    from, rounded to 6 decimals, with the input rows never mutated.  Any other
    sum is left untouched: an out-of-band total is an incomplete-holdings
    problem, not a scale problem.  With ``already_normalized=True`` the rows
    are never rescaled regardless of the sum - double-scaling is exactly the
    bug this flag guards.
    """
    try:
        if not isinstance(rows, (list, tuple)) or not rows:
            return _failure(
                CODE_SCALE_FRACTION_NAV,
                "rows must be a non-empty sequence of holding mappings",
            )
        parsed: list[tuple[Mapping[str, object], float, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                return _failure(
                    CODE_SCALE_FRACTION_NAV,
                    f"holding row is not a mapping: {type(row).__name__}",
                )
            weight = _row_weight(row)
            if weight is None:
                return _failure(
                    CODE_SCALE_FRACTION_NAV,
                    f"holding row has no parseable weight: {row!r}",
                )
            parsed.append((row, weight[0], weight[1]))
        original_total = round(sum(w for _, w, _ in parsed), SCALE_DECIMALS)
        if not already_normalized and FRACTION_MIN <= original_total <= FRACTION_MAX:
            scaled = [
                (row, round(w * SCALE_FACTOR, SCALE_DECIMALS), key)
                for row, w, key in parsed
            ]
            fixed_rows: list[dict[str, object]] = []
            for row, weight, key in scaled:
                fixed = dict(row)
                fixed[key] = weight
                fixed_rows.append(fixed)
            new_total = round(sum(w for _, w, _ in scaled), SCALE_DECIMALS)
            info = {
                "scaled": True,
                "original_total": original_total,
                "new_total": new_total,
                "factor": SCALE_FACTOR,
                "n_rows": len(fixed_rows),
            }
            return fixed_rows, info
        info = {
            "scaled": False,
            "original_total": original_total,
            "new_total": original_total,
            "factor": 1.0,
            "n_rows": len(parsed),
        }
        return list(rows), info
    except Exception as exc:
        return _failure(
            CODE_SCALE_FRACTION_NAV,
            f"unrecoverable input: {type(exc).__name__}: {exc}",
        )


def _pdf_page_texts(pdf_path: Path) -> list[str]:
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        return [doc[i].get_text() or "" for i in range(doc.page_count)]
    finally:
        doc.close()


def _resolve_page_texts(pdf_path_or_pages: object, page_texts: object) -> list[str] | None:
    if page_texts is not None:
        if isinstance(page_texts, (list, tuple)):
            return list(page_texts)
        return None
    if isinstance(pdf_path_or_pages, (list, tuple)):
        return list(pdf_path_or_pages)
    if isinstance(pdf_path_or_pages, (str, Path)):
        return _pdf_page_texts(Path(pdf_path_or_pages))
    return None


def rank_pdf_pages(
    pdf_path_or_pages: str | Path | Sequence[str] | None = None,
    *,
    max_pages: int = 6,
    page_texts: Sequence[str] | None = None,
) -> tuple[list[int] | None, dict[str, object]]:
    """Fix ``ERR_PDF_HIGH_PAGE_DENSITY``: rank pages by holdings-table density.

    Score per page (mirrors ``scripts/linksheet_sync.py::ai_extract_pdf``):
    ``3 * ISIN hits + min(percent-value hits, 20) + holdings-keyword hits``;
    pages with fewer than 40 characters of text are skipped as blank/cover
    pages.  Returns the top ``max_pages`` page indices in ASCENDING order;
    when no page scores above zero (scanned PDF, no text layer) the first
    ``max_pages`` indices are returned with ``info["fallback"] = True`` - the
    proven graceful degradation.  ``page_texts`` (positional list or keyword)
    injects page text for tests and bypasses the filesystem entirely; a
    str/Path source is opened with pymupdf (lazy import).
    """
    try:
        k = max(1, int(max_pages))
        texts = _resolve_page_texts(pdf_path_or_pages, page_texts)
        if texts is None:
            return _failure(
                CODE_PDF_HIGH_PAGE_DENSITY,
                "source must be a PDF path or a sequence of page texts",
            )
        if not texts:
            return _failure(CODE_PDF_HIGH_PAGE_DENSITY, "no pages to rank")
        scored: list[tuple[int, int]] = []
        for i, text in enumerate(texts):
            if not isinstance(text, str):
                return _failure(CODE_PDF_HIGH_PAGE_DENSITY, f"page {i} text is not a string")
            if len(text.strip()) < MIN_PAGE_CHARS:
                continue
            score = (
                len(ISIN_RE.findall(text)) * 3
                + min(len(PCT_LINE_RE.findall(text)), MAX_PCT_HITS)
                + len(HOLDINGS_KW_RE.findall(text))
            )
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        pages = sorted(i for _, i in scored[:k])
        fallback = not pages
        if fallback:
            pages = list(range(min(len(texts), k)))
        info = {
            "n_pages": len(texts),
            "scored_pages": len(scored),
            "max_pages": k,
            "scores": {i: s for s, i in scored},
            "fallback": fallback,
        }
        return pages, info
    except Exception as exc:
        return _failure(
            CODE_PDF_HIGH_PAGE_DENSITY,
            f"unrecoverable input: {type(exc).__name__}: {exc}",
        )


def _workbook_sheet_texts(workbook_path: Path, head_rows: int) -> dict[str, str]:
    import pandas as pd

    xl = pd.ExcelFile(workbook_path)
    texts: dict[str, str] = {}
    for name in xl.sheet_names:
        try:
            head = xl.parse(name, nrows=head_rows)
        except Exception:
            continue
        texts[name] = head.to_csv(index=False)
    return texts


def _resolve_sheet_texts(
    workbook_path_or_sheets: object,
    sheet_texts: object,
    head_rows: int,
) -> dict[str, str] | None:
    if sheet_texts is not None:
        if isinstance(sheet_texts, Mapping):
            return dict(sheet_texts)
        return None
    if isinstance(workbook_path_or_sheets, Mapping):
        return dict(workbook_path_or_sheets)
    if isinstance(workbook_path_or_sheets, (str, Path)):
        return _workbook_sheet_texts(Path(workbook_path_or_sheets), head_rows)
    return None


def select_sheets(
    workbook_path_or_sheets: str | Path | Mapping[str, str] | None = None,
    *,
    max_sheets: int = 3,
    rows: int = 25,
    sheet_texts: Mapping[str, str] | None = None,
) -> tuple[list[str] | None, dict[str, object]]:
    """Fix ``ERR_WORKBOOK_MULTI_SHEET``: rank sheets by holdings-table density.

    Score per sheet (mirrors ``scripts/linksheet_sync.py::ai_extract_sheet``):
    ``keyword hits (isin|portfolio|holding|% to nav|instrument|company) +
    3 * ISIN hits``.  Returns the top ``max_sheets`` sheet names in rank order
    (score descending, name ascending on ties); when nothing scores the first
    two sheet names are returned with ``info["fallback"] = True``.  For a real
    workbook (str/Path, pandas lazy import) each sheet's first ``rows`` rows
    are flattened to CSV text before scoring; ``sheet_texts`` (positional
    mapping or keyword) injects that flattened text for tests.
    """
    try:
        k = max(1, int(max_sheets))
        head_rows = max(1, int(rows))
        texts = _resolve_sheet_texts(workbook_path_or_sheets, sheet_texts, head_rows)
        if texts is None:
            return _failure(
                CODE_WORKBOOK_MULTI_SHEET,
                "source must be a workbook path or a mapping of sheet name to text",
            )
        if not texts:
            return _failure(CODE_WORKBOOK_MULTI_SHEET, "no readable sheets in workbook")
        scored: list[tuple[int, str]] = []
        for name, text in texts.items():
            if not isinstance(name, str) or not isinstance(text, str):
                return _failure(
                    CODE_WORKBOOK_MULTI_SHEET,
                    f"sheet {name!r} must have a string name and string text",
                )
            score = len(SHEET_KW_RE.findall(text)) + len(ISIN_RE.findall(text)) * 3
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda t: (-t[0], t[1]))
        picks = [name for _, name in scored[:k]]
        fallback = not picks
        if fallback:
            picks = list(texts)[: min(2, k)]
        info = {
            "n_sheets": len(texts),
            "scored_sheets": len(scored),
            "max_sheets": k,
            "head_rows": head_rows,
            "scores": {name: s for s, name in scored},
            "fallback": fallback,
        }
        return picks, info
    except Exception as exc:
        return _failure(
            CODE_WORKBOOK_MULTI_SHEET,
            f"unrecoverable input: {type(exc).__name__}: {exc}",
        )


def evp_bytes_to_key(
    password: str | bytes,
    salt: bytes | None,
    key_len: int = AES_KEY_LEN,
    iv_len: int = AES_IV_LEN,
) -> tuple[bytes, bytes]:
    """OpenSSL ``EVP_BytesToKey`` (MD5, count=1) - pure and deterministic.

    Derives ``key_len`` key bytes followed by ``iv_len`` IV bytes by chaining
    ``D_i = MD5(D_{i-1} || password || salt)`` (``D_0`` empty) until
    ``key_len + iv_len`` bytes are collected - the exact KDF behind the
    ``Salted__`` envelope of ``ERR_ENCRYPTED_PAYLOAD_AES``.  ``salt=None`` is
    the unsalted OpenSSL mode.
    """
    if key_len < 0 or iv_len < 0:
        raise ValueError("key_len and iv_len must be non-negative")
    pwd = _as_bytes(password, "password")
    slt = b"" if salt is None else _as_bytes(salt, "salt")
    material = b""
    prev = b""
    while len(material) < key_len + iv_len:
        prev = hashlib.md5(prev + pwd + slt).digest()
        material += prev
    return material[:key_len], material[key_len:key_len + iv_len]


def decrypt_payload(
    blob: bytes | bytearray | memoryview,
    password: str | bytes,
    *,
    decryptor: Callable[[bytes, bytes, bytes], bytes] | None = None,
) -> tuple[bytes | None, dict[str, object]]:
    """Fix ``ERR_ENCRYPTED_PAYLOAD_AES``: parse the OpenSSL ``Salted__`` envelope.

    Layout: ``b"Salted__"`` (8 bytes) || salt (8 bytes) || AES-256-CBC
    ciphertext (a non-zero multiple of 16 bytes).  The key/IV are derived with
    :func:`evp_bytes_to_key` (MD5, 32-byte key, 16-byte IV) and decryption is
    DELEGATED to the injected ``decryptor(ciphertext, key, iv) -> bytes``
    callable - this module never implements AES; the repo's real cipher path
    (Edelweiss adapter) supplies it.  Without a decryptor the envelope is
    still parsed and a typed failure is returned, so callers can distinguish
    "not an envelope" from "no cipher available".  Returns
    ``(plaintext_bytes_or_None, info)``.
    """
    try:
        if isinstance(blob, str) or not isinstance(blob, (bytes, bytearray, memoryview)):
            return _failure(CODE_ENCRYPTED_PAYLOAD_AES, "blob must be bytes-like, not str")
        data = bytes(blob)
        if not data.startswith(SALT_MAGIC):
            return _failure(
                CODE_ENCRYPTED_PAYLOAD_AES,
                "payload does not start with the OpenSSL Salted__ magic",
            )
        if len(data) < len(SALT_MAGIC) + SALT_LEN + AES_BLOCK_LEN:
            return _failure(
                CODE_ENCRYPTED_PAYLOAD_AES,
                f"truncated Salted__ envelope: {len(data)} bytes",
            )
        salt = data[len(SALT_MAGIC):len(SALT_MAGIC) + SALT_LEN]
        ciphertext = data[len(SALT_MAGIC) + SALT_LEN:]
        if len(ciphertext) % AES_BLOCK_LEN != 0:
            return _failure(
                CODE_ENCRYPTED_PAYLOAD_AES,
                f"ciphertext length {len(ciphertext)} is not an AES block multiple",
            )
        if decryptor is None:
            return None, {
                "error": "no AES decryptor injected; the real cipher path (Edelweiss adapter) supplies it",
                "failure_code": CODE_ENCRYPTED_PAYLOAD_AES,
                "salt_hex": salt.hex(),
            }
        if not callable(decryptor):
            return _failure(CODE_ENCRYPTED_PAYLOAD_AES, "decryptor must be callable")
        key, iv = evp_bytes_to_key(password, salt, AES_KEY_LEN, AES_IV_LEN)
        plaintext = decryptor(ciphertext, key, iv)
        if isinstance(plaintext, str) or not isinstance(plaintext, (bytes, bytearray, memoryview)):
            return _failure(
                CODE_ENCRYPTED_PAYLOAD_AES,
                f"decryptor returned {type(plaintext).__name__}, expected bytes",
            )
        plaintext = bytes(plaintext)
        info = {
            "salt_hex": salt.hex(),
            "kdf": "evp_bytes_to_key_md5",
            "cipher": "aes-256-cbc",
            "key_len": AES_KEY_LEN,
            "iv_len": AES_IV_LEN,
            "plaintext_len": len(plaintext),
        }
        return plaintext, info
    except Exception as exc:
        return _failure(
            CODE_ENCRYPTED_PAYLOAD_AES,
            f"unrecoverable input: {type(exc).__name__}: {exc}",
        )


__all__ = [
    "AES_BLOCK_LEN",
    "AES_IV_LEN",
    "AES_KEY_LEN",
    "CODE_ENCRYPTED_PAYLOAD_AES",
    "CODE_PDF_HIGH_PAGE_DENSITY",
    "CODE_SCALE_FRACTION_NAV",
    "CODE_WORKBOOK_MULTI_SHEET",
    "FRACTION_MAX",
    "FRACTION_MIN",
    "HOLDINGS_KW_RE",
    "ISIN_RE",
    "MAX_PCT_HITS",
    "MIN_PAGE_CHARS",
    "PCT_LINE_RE",
    "SALT_LEN",
    "SALT_MAGIC",
    "SCALE_DECIMALS",
    "SCALE_FACTOR",
    "SHEET_KW_RE",
    "decrypt_payload",
    "evp_bytes_to_key",
    "normalize_scale",
    "rank_pdf_pages",
    "select_sheets",
]
