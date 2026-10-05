"""T1: fixed failure taxonomy - exactly 15 stable codes, immutable per-code
metadata, and a conservative exception classifier that never guesses."""

from __future__ import annotations

import ast
import json
import ssl
import sys
import zipfile
from pathlib import Path

import pytest

from src.agents import taxonomy
from src.agents.taxonomy import (
    FAILURE_CODES,
    FAILURE_METADATA,
    FailureCodeInfo,
    classify_exception,
    is_valid_code,
)

SPEC_CODES = (
    "ERR_WAF_CLOUDFLARE_1015",
    "ERR_CONSENT_ROTATING_LABELS",
    "ERR_DOM_GATED_INLINE_URLS",
    "ERR_AUTH_BEARER_CMS",
    "ERR_ENCRYPTED_PAYLOAD_AES",
    "ERR_WORKBOOK_MULTI_SHEET",
    "ERR_PDF_HIGH_PAGE_DENSITY",
    "ERR_ARCHIVE_ZIP_SINGLE_XLS",
    "ERR_LLM_EMPTY_REASONING",
    "ERR_SCALE_FRACTION_NAV",
    "ERR_HOLDINGS_INCOMPLETE_SUM",
    "ERR_HOLDINGS_TOPN_ONLY",
    "ERR_HOLDINGS_PARSER_PARTIAL",
    "ERR_SCHEME_MISSING_IN_DB",
    "ERR_SCALE_SUSPECT_WEIGHTS",
)


def test_exactly_fifteen_codes_in_spec_order():
    assert len(FAILURE_CODES) == 15
    assert len(set(FAILURE_CODES)) == 15
    assert tuple(FAILURE_CODES) == SPEC_CODES


def test_taxonomy_is_immutable():
    assert isinstance(FAILURE_CODES, tuple)
    with pytest.raises(TypeError):
        FAILURE_METADATA["ERR_SCALE_FRACTION_NAV"] = None  # type: ignore[index]
    info = FAILURE_METADATA["ERR_SCALE_FRACTION_NAV"]
    assert isinstance(info, FailureCodeInfo)
    assert isinstance(info.ladder, tuple)
    with pytest.raises(AttributeError):
        info.code = "ERR_MUTATED"


def test_metadata_covers_every_code_with_summary_and_ladder():
    assert set(FAILURE_METADATA) == set(SPEC_CODES)
    for code in FAILURE_CODES:
        info = FAILURE_METADATA[code]
        assert info.code == code
        assert info.summary.strip()
        assert info.remediation.strip()
        assert isinstance(info.ladder, tuple) and len(info.ladder) > 0
        assert all(step.strip() and step == step.lower() for step in info.ladder)


def test_is_valid_code():
    for code in SPEC_CODES:
        assert is_valid_code(code) is True
    assert is_valid_code("ERR_NOT_A_CODE") is False
    assert is_valid_code("") is False
    assert is_valid_code(None) is False
    assert is_valid_code("err_waf_cloudflare_1015") is False


def _http_status_error(httpx, status: int, body: str = ""):
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=httpx.Request("GET", "https://amc.example.com/factsheet"),
        response=httpx.Response(status, text=body),
    )


def test_classify_http_status_errors():
    httpx = pytest.importorskip("httpx")
    assert classify_exception(_http_status_error(httpx, 429)) == "ERR_WAF_CLOUDFLARE_1015"
    assert classify_exception(_http_status_error(httpx, 403, "error code: 1015")) == "ERR_WAF_CLOUDFLARE_1015"
    assert classify_exception(_http_status_error(httpx, 403, "cloudflare block page")) == "ERR_WAF_CLOUDFLARE_1015"
    assert classify_exception(_http_status_error(httpx, 401)) == "ERR_AUTH_BEARER_CMS"
    assert classify_exception(_http_status_error(httpx, 403)) == "ERR_AUTH_BEARER_CMS"
    assert classify_exception(_http_status_error(httpx, 500)) is None
    assert classify_exception(_http_status_error(httpx, 404)) is None


def test_classify_transport_timeout_and_tls_errors_stay_unclassified():
    httpx = pytest.importorskip("httpx")
    assert classify_exception(httpx.TimeoutException("read timed out")) is None
    assert classify_exception(httpx.ConnectError("connection refused")) is None
    assert classify_exception(ssl.SSLError("TLS handshake failed")) is None
    assert classify_exception(TimeoutError("timed out")) is None
    assert classify_exception(ConnectionError("connection reset by peer")) is None


def test_classify_zip_json_and_llm_errors():
    assert classify_exception(zipfile.BadZipFile("not a zip archive")) == "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    assert classify_exception(zipfile.LargeZipFile("zip64 required")) == "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    with pytest.raises(json.JSONDecodeError) as ei:
        json.loads("{not json")
    assert classify_exception(ei.value) == "ERR_ENCRYPTED_PAYLOAD_AES"
    from src.ai_extract import ExtractError

    assert classify_exception(ExtractError("empty reasoning body")) == "ERR_LLM_EMPTY_REASONING"


def test_classify_extract_error_empty_body_messages_map_to_empty_reasoning():
    from src.ai_extract import ExtractError

    assert (
        classify_exception(ExtractError("AI returned no usable holdings rows"))
        == "ERR_LLM_EMPTY_REASONING"
    )
    assert (
        classify_exception(ExtractError("opencode returned an empty response body"))
        == "ERR_LLM_EMPTY_REASONING"
    )


def test_classify_extract_error_transport_and_rejections_stay_unclassified():
    from src.ai_extract import ExtractError

    assert classify_exception(ExtractError("provider rejected request: HTTP 400 Bad Request")) is None
    assert classify_exception(ExtractError("provider rejected request: HTTP 401 Unauthorized")) is None
    assert classify_exception(ExtractError("AI provider unreachable: connection timed out")) is None
    assert classify_exception(ExtractError("AI provider unreachable: HTTP 500")) is None
    assert classify_exception(ExtractError("openrouter returned a non-JSON body")) is None
    assert (
        classify_exception(ExtractError("AI extraction not configured (ai.enabled / key env)"))
        is None
    )


def test_classify_cloudflare_1015_from_plain_message():
    assert classify_exception(RuntimeError("blocked: error code: 1015")) == "ERR_WAF_CLOUDFLARE_1015"
    assert classify_exception(RuntimeError("cloudflare denied us with 1015")) == "ERR_WAF_CLOUDFLARE_1015"


def test_classify_unknown_errors_return_none():
    assert classify_exception(None) is None
    assert classify_exception(ValueError("boom")) is None
    assert classify_exception(KeyError("scheme")) is None
    assert classify_exception(RuntimeError("parser exploded")) is None


def test_classify_never_returns_an_invalid_code():
    probes = [
        None,
        ValueError("x"),
        zipfile.BadZipFile("x"),
        RuntimeError("error code: 1015"),
    ]
    try:
        import httpx

        probes.extend([
            _http_status_error(httpx, 429),
            _http_status_error(httpx, 401),
            _http_status_error(httpx, 503),
            httpx.ConnectError("x"),
        ])
    except ImportError:
        pass
    for exc in probes:
        code = classify_exception(exc)
        assert code is None or is_valid_code(code)


def test_ladder_semantics_match_spec():
    waf = FAILURE_METADATA["ERR_WAF_CLOUDFLARE_1015"]
    assert "circuit_breaker" in waf.ladder[0]
    topn = FAILURE_METADATA["ERR_HOLDINGS_TOPN_ONLY"]
    assert "never_escalate" in topn.ladder
    suspect = FAILURE_METADATA["ERR_SCALE_SUSPECT_WEIGHTS"]
    assert suspect.ladder[0] == "always_escalate"
    incomplete = FAILURE_METADATA["ERR_HOLDINGS_INCOMPLETE_SUM"]
    assert incomplete.ladder[0].startswith("amc_recheck")
    assert incomplete.ladder[-1].startswith("manual")
    fraction = FAILURE_METADATA["ERR_SCALE_FRACTION_NAV"]
    assert "multiply_parsed_holdings_by_100" in fraction.ladder


def test_package_init_reexports_taxonomy_public_names():
    import src.agents as pkg

    assert pkg.FAILURE_CODES is taxonomy.FAILURE_CODES
    assert pkg.FAILURE_METADATA is taxonomy.FAILURE_METADATA
    assert pkg.FailureCodeInfo is taxonomy.FailureCodeInfo
    assert pkg.classify_exception is taxonomy.classify_exception
    assert pkg.is_valid_code is taxonomy.is_valid_code
    assert set(pkg.__all__) == {
        "FAILURE_CODES",
        "FAILURE_METADATA",
        "FailureCodeInfo",
        "classify_exception",
        "is_valid_code",
    }


def test_package_init_imports_no_heavy_modules():
    import src.agents as pkg

    tree = ast.parse(Path(pkg.__file__).read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            targets.add("." * node.level + (node.module or ""))
    assert "src.agents.taxonomy" in targets
    for target in targets:
        top = target.lstrip(".").split(".")[0]
        assert top in ("src", "__future__") or top in sys.stdlib_module_names, target
        assert top not in ("webapp", "playwright", "pandas", "httpx", "fastapi"), target
