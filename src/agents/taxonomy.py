"""Fixed failure taxonomy for the AMC holdings agents (SPEC section 6).

Every failed operation in the agent framework must be tagged with exactly one
of the 15 stable codes defined here (AC-5). The taxonomy is immutable: the
code list, the per-code metadata and the default remediation ladders are
frozen at import time and derived from the SPEC section 6 table - adding or
renaming codes requires a spec change, never a local edit.

``classify_exception`` is a conservative, context-free first pass: it maps an
exception to a code only when the taxonomy has a genuine home for that failure
family, and returns ``None`` otherwise. Callers must treat ``None`` as
unclassified and never guess. Network timeouts, TLS handshake failures and
DNS/connection errors deliberately have no code: the fixed taxonomy owns
documented failure modes, while transient transport faults belong to the
strategy ladder and the rate limiter, not to a failure code.
"""

from __future__ import annotations

import json
import logging
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailureCodeInfo:
    """Immutable per-code metadata derived from the SPEC section 6 table."""

    code: str
    summary: str
    remediation: str
    ladder: tuple[str, ...]


_METADATA: dict[str, FailureCodeInfo] = {
    "ERR_WAF_CLOUDFLARE_1015": FailureCodeInfo(
        code="ERR_WAF_CLOUDFLARE_1015",
        summary="Cloudflare rate-limit temporary IP ban (HTTP 1015) on the AMC host (360 ONE).",
        remediation=(
            "Trip the 30-minute host circuit breaker, stop all requests to the host and "
            "retry once after backoff; never rotate IPs, spoof identities or evade the ban."
        ),
        ladder=(
            "trip_30min_host_circuit_breaker",
            "stop_all_requests_to_host",
            "retry_once_after_backoff",
            "never_evade_or_spoof_identity",
        ),
    ),
    "ERR_CONSENT_ROTATING_LABELS": FailureCodeInfo(
        code="ERR_CONSENT_ROTATING_LABELS",
        summary="Dynamic consent modal dialogs with rotating button labels (Agree/Disagree vs Yes/No) gate the DOM (360 ONE, Tata).",
        remediation=(
            "Fuzzy-regex click the positive consent tokens (Agree|Accept|Proceed) and "
            "dismiss the overlay before the DOM wait."
        ),
        ladder=(
            "fuzzy_regex_click_positive_consent_tokens",
            "dismiss_overlay_before_dom_wait",
        ),
    ),
    "ERR_DOM_GATED_INLINE_URLS": FailureCodeInfo(
        code="ERR_DOM_GATED_INLINE_URLS",
        summary="Document links are injected into script tags instead of <a> tags (Invesco, Nippon).",
        remediation=(
            "After consent dismissal, regex-sweep the raw HTML/DOM for document URLs "
            "(pdf/xlsx/xls/csv/zip)."
        ),
        ladder=(
            "dismiss_consent_overlay",
            "regex_sweep_raw_html_for_document_urls",
            "collect_pdf_xlsx_xls_csv_zip_links",
        ),
    ),
    "ERR_AUTH_BEARER_CMS": FailureCodeInfo(
        code="ERR_AUTH_BEARER_CMS",
        summary="CMS API rejects plain calls without the browser-issued Authorization: Bearer token (Axis MF).",
        remediation=(
            "Intercept the Authorization: Bearer header with a headless Playwright context "
            "on initial load, then replay the API calls via httpx."
        ),
        ladder=(
            "open_headless_playwright_context",
            "intercept_authorization_bearer_header",
            "replay_api_calls_via_httpx",
        ),
    ),
    "ERR_ENCRYPTED_PAYLOAD_AES": FailureCodeInfo(
        code="ERR_ENCRYPTED_PAYLOAD_AES",
        summary="Akamai AES-256-CBC encrypted API responses; the payload is not plain JSON (Edelweiss).",
        remediation=(
            "Compute the dynamic HMAC session key and decrypt the Salted__ ciphertext via "
            "the OpenSSL EVP_BytesToKey routine."
        ),
        ladder=(
            "compute_dynamic_hmac_session_key",
            "decrypt_salted_ciphertext_evp_bytestokey",
            "parse_decrypted_json_payload",
        ),
    ),
    "ERR_WORKBOOK_MULTI_SHEET": FailureCodeInfo(
        code="ERR_WORKBOOK_MULTI_SHEET",
        summary="Consolidated workbooks with 100+ sheets hide the target scheme sheets (HDFC, SBI).",
        remediation=(
            "Inspect sheet metadata and filter the target scheme sheets via fuzzy name "
            "matching and column-header analysis."
        ),
        ladder=(
            "inspect_sheet_metadata",
            "fuzzy_match_scheme_sheet_names",
            "validate_column_headers",
            "route_target_sheets_to_parser",
        ),
    ),
    "ERR_PDF_HIGH_PAGE_DENSITY": FailureCodeInfo(
        code="ERR_PDF_HIGH_PAGE_DENSITY",
        summary="Very large factsheets (150+ pages) overflow the parser timeout (ICICI, Kotak).",
        remediation=(
            "Pre-scan page text, rank pages by ISIN count and '% to NAV' keyword density, "
            "then parse only the top-N ranked pages."
        ),
        ladder=(
            "pre_scan_page_text",
            "rank_pages_by_isin_count_and_nav_keyword_density",
            "parse_top_n_ranked_pages",
        ),
    ),
    "ERR_ARCHIVE_ZIP_SINGLE_XLS": FailureCodeInfo(
        code="ERR_ARCHIVE_ZIP_SINGLE_XLS",
        summary="The .zip archive contains a lone spreadsheet instead of per-scheme documents (Bandhan, Quant).",
        remediation=(
            "Auto-extract via src/zip_parser.py, sanitize the unpacked file extensions and "
            "route the inner member to the parser."
        ),
        ladder=(
            "extract_archive_via_zip_parser",
            "sanitize_unpacked_file_extensions",
            "route_inner_member_to_parser",
        ),
    ),
    "ERR_LLM_EMPTY_REASONING": FailureCodeInfo(
        code="ERR_LLM_EMPTY_REASONING",
        summary="Reasoning LLM exhausts its token budget and returns an empty body (GLM 5.3).",
        remediation=(
            "Catch the empty body, short-circuit the retry counter and fall back to the "
            "local opencode/space-bunny-free CLI."
        ),
        ladder=(
            "catch_empty_body",
            "short_circuit_retry_counter",
            "fallback_local_opencode_cli",
        ),
    ),
    "ERR_SCALE_FRACTION_NAV": FailureCodeInfo(
        code="ERR_SCALE_FRACTION_NAV",
        summary="Scheme weightings reported as fractions (0.02) instead of percent (2.0%).",
        remediation=(
            "Check the weight sum; when 0.90 <= sum <= 1.10 multiply all parsed holding "
            "values by 100.0."
        ),
        ladder=(
            "check_sum_weight_in_0_90_to_1_10",
            "multiply_parsed_holdings_by_100",
            "revalidate_sum_in_95_to_105",
        ),
    ),
    "ERR_HOLDINGS_INCOMPLETE_SUM": FailureCodeInfo(
        code="ERR_HOLDINGS_INCOMPLETE_SUM",
        summary="A full-disclosure document was parsed but the weight sum is below 95% of NAV (rows dropped by the parser).",
        remediation=(
            "Escalate (T2) through the escalation channel order until a full-disclosure "
            "parse reaches a weight sum of at least 95%."
        ),
        ladder=(
            "amc_recheck_alternate_locations_and_new_parse_strategy",
            "web_search_allow_listed_hosts_only",
            "amfi_source_agent",
            "advisorkhoj_source_agent",
            "manual_intervention_register",
        ),
    ),
    "ERR_HOLDINGS_TOPN_ONLY": FailureCodeInfo(
        code="ERR_HOLDINGS_TOPN_ONLY",
        summary="Only a factsheet (inherently top-10) document exists for the scheme-month.",
        remediation=(
            "Accept as tier T1 TOP10_FALLBACK, tag the record 'Top 10 only' and do NOT "
            "escalate; optionally try one T0 hunt via the AMC agent."
        ),
        ladder=(
            "accept_tier_t1_top10_fallback",
            "tag_record_top_10_only",
            "optional_single_t0_hunt_via_amc_agent",
            "never_escalate",
        ),
    ),
    "ERR_HOLDINGS_PARSER_PARTIAL": FailureCodeInfo(
        code="ERR_HOLDINGS_PARSER_PARTIAL",
        summary="Parser read a workbook/PDF but dropped rows (merged headers, multi-sheet, spanned pages).",
        remediation=(
            "Re-parse with the sheet/page-ranking strategies; escalate (T2) if the holding "
            "set is still incomplete."
        ),
        ladder=(
            "reparse_with_multi_sheet_strategy",
            "reparse_with_page_ranking_strategy",
            "escalate_t2_if_still_incomplete",
        ),
    ),
    "ERR_SCHEME_MISSING_IN_DB": FailureCodeInfo(
        code="ERR_SCHEME_MISSING_IN_DB",
        summary="A scheme in the AMFI NAV universe has no holdings row in the webapp DB for the latest month.",
        remediation=(
            "Escalate (T2): the discovery agent re-searches and the source agents "
            "cross-check; park MANUAL with NOT_PUBLISHED_BY_AMC if genuinely undisclosed."
        ),
        ladder=(
            "discovery_agent_re_search",
            "amfi_source_agent_cross_check",
            "advisorkhoj_source_agent_cross_check",
            "park_manual_not_published_by_amc_if_undisclosed",
        ),
    ),
    "ERR_SCALE_SUSPECT_WEIGHTS": FailureCodeInfo(
        code="ERR_SCALE_SUSPECT_WEIGHTS",
        summary="Weight sum above 105% or a single weight above 100% (fraction/percent scale bug or double-count).",
        remediation=(
            "Always escalate; re-parse with the ERR_SCALE_FRACTION_NAV normalization and "
            "never display until fixed."
        ),
        ladder=(
            "always_escalate",
            "reparse_with_scale_fraction_nav_normalization",
            "never_display_until_fixed",
        ),
    ),
}

FAILURE_CODES: tuple[str, ...] = tuple(_METADATA)

if len(FAILURE_CODES) != 15 or len(set(FAILURE_CODES)) != 15:
    raise RuntimeError("failure taxonomy must define exactly 15 unique codes")

FAILURE_METADATA: Mapping[str, FailureCodeInfo] = MappingProxyType(_METADATA)

_CODE_SET = frozenset(FAILURE_CODES)


def is_valid_code(code: object) -> bool:
    """Return True only for one of the 15 stable taxonomy codes."""
    return isinstance(code, str) and code in _CODE_SET


def classify_exception(exc: BaseException | None) -> str | None:
    """Map a caught exception to its taxonomy code; ``None`` when unclassified.

    Context-free first pass over the common failure families: HTTP 429 and
    Cloudflare 1015 blocks map to ``ERR_WAF_CLOUDFLARE_1015``, HTTP 401/403 to
    ``ERR_AUTH_BEARER_CMS``, zip-archive errors to
    ``ERR_ARCHIVE_ZIP_SINGLE_XLS`` and JSON decode failures on a payload to
    ``ERR_ENCRYPTED_PAYLOAD_AES``. An ``ai_extract.ExtractError`` maps to
    ``ERR_LLM_EMPTY_REASONING`` only when its message indicates an empty model
    body or an empty parse result (the message contains "empty" or "no usable
    holdings rows"); every other ``ExtractError`` - provider unreachable,
    provider rejected the request, non-JSON body, extraction not configured -
    is a transport/auth/config fault with no home in the fixed taxonomy and
    returns ``None``. Everything else - including network timeouts, TLS
    handshake failures and connection errors - returns ``None`` so callers
    treat it as unclassified instead of guessing.
    """
    if exc is None:
        return None
    msg = str(exc).lower()
    if "error code: 1015" in msg or ("cloudflare" in msg and "1015" in msg):
        return "ERR_WAF_CLOUDFLARE_1015"
    try:
        import httpx
    except Exception:
        httpx = None
    if httpx is not None and isinstance(exc, httpx.HTTPError):
        if isinstance(exc, httpx.HTTPStatusError):
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            try:
                body = exc.response.text.lower()
            except Exception:
                body = ""
            if status == 429 or "error code: 1015" in body:
                return "ERR_WAF_CLOUDFLARE_1015"
            if status in (401, 403):
                if "cloudflare" in body:
                    return "ERR_WAF_CLOUDFLARE_1015"
                return "ERR_AUTH_BEARER_CMS"
        return None
    if isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile)):
        return "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    if isinstance(exc, json.JSONDecodeError):
        return "ERR_ENCRYPTED_PAYLOAD_AES"
    try:
        from src.ai_extract import ExtractError
    except Exception:
        ExtractError = None
    if ExtractError is not None and isinstance(exc, ExtractError):
        if "empty" in msg or "no usable holdings rows" in msg:
            return "ERR_LLM_EMPTY_REASONING"
        return None
    logger.debug("unclassified exception %s: %s", type(exc).__name__, exc)
    return None


__all__ = [
    "FAILURE_CODES",
    "FAILURE_METADATA",
    "FailureCodeInfo",
    "classify_exception",
    "is_valid_code",
]
