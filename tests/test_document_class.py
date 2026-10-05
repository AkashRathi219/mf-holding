"""[T31] document_class classification + save-time stamping tests.

Covers the plan's four cases (factsheet-named / grouped_factsheet ->
``factsheet_topn``, fortnightly name -> ``fortnightly``, monthly-portfolio
payload -> ``full_portfolio``, unclassifiable -> ``unknown``), the legacy
payload contract (no field -> ``unknown``, the strict-rule default), the
signal-conflict precedence, determinism, and the additive ``document_class``
stamp written by the four parser ``save_*`` functions.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.document_class import (
    DOCUMENT_CLASSES,
    FACTSHEET_TOPN,
    FORTNIGHTLY,
    FULL_PORTFOLIO,
    UNKNOWN,
    classify,
)
from src.excel_parser import save_excel_parsed_data
from src.html_parser import save_html_parsed_data
from src.pdf_parser import save_parsed_data
from src.zip_parser import save_zip_parsed_data


def _excel_payload(source_file: str, scheme_name: str = "Some Fund") -> dict:
    return {
        "source_file": source_file,
        "file_type": "excel",
        "metadata": {},
        "schemes": {
            "Sheet1": {
                "scheme_name": scheme_name,
                "fund_name": scheme_name,
                "date": "31 July 2026",
                "holdings": [],
                "sectors": [],
            }
        },
    }


# ---------------------------------------------------------------------------
# the plan's four cases
# ---------------------------------------------------------------------------

def test_factsheet_named_payload_is_factsheet_topn():
    payload = _excel_payload(
        r"data\raw\pdfs\Abakkus_Mutual_Fund\2026\07\Abakkus_Mutual_Fund_Factsheet_July_2026.pdf")
    assert classify(payload) == FACTSHEET_TOPN


def test_grouped_factsheet_metadata_is_factsheet_topn():
    payload = {
        "source_file": r"data\raw\pdfs\Zerodha_Mutual_Fund\2026\06\Factsheet - Jun 26.pdf",
        "file_type": "pdf",
        "metadata": {"grouped_factsheet": True, "total_schemes": 16},
        "schemes": {},
    }
    assert classify(payload) == FACTSHEET_TOPN


def test_grouped_factsheet_filename_is_factsheet_topn():
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\grouped_factsheet_July_2026.pdf")
    assert classify(payload) == FACTSHEET_TOPN


def test_fortnightly_name_is_fortnightly():
    payload = _excel_payload(
        r"data\raw\pdfs\ASK\2026\09\ASK_Liquid_Fund_Fortnightly_Portfolio_Disclosures_15092026.xlsx")
    assert classify(payload) == FORTNIGHTLY


def test_monthly_portfolio_payload_is_full_portfolio():
    payload = _excel_payload(
        r"data\raw\pdfs\CMLIQ\2026\07\CMLIQ_Monthly_Portfolio_Disclosure_July_31_2026.xlsx")
    assert classify(payload) == FULL_PORTFOLIO


def test_unclassifiable_payload_is_unknown():
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\Revised_Disclosure_8f903ba45c.pdf")
    assert classify(payload) == UNKNOWN


def test_empty_payload_is_unknown():
    assert classify({}) == UNKNOWN


# ---------------------------------------------------------------------------
# legacy payloads (no document_class field) load as unknown
# ---------------------------------------------------------------------------

def test_legacy_payload_without_field_is_unknown(tmp_path):
    legacy = {
        "source_file": r"data\raw\pdfs\AMC\2026\07\Website_Disclosure_a3357cda20.pdf",
        "file_type": "pdf",
        "metadata": {"source_sha256": "abc123"},
        "schemes": {},
    }
    assert "document_class" not in legacy
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded.get("document_class", UNKNOWN) == UNKNOWN
    assert classify(loaded) == UNKNOWN


def test_legacy_payload_with_signals_still_classifies():
    legacy = {
        "source_file": r"data\raw\pdfs\AMC\2026\07\Monthly_Portfolio_Notice_64ca2d8bda.pdf",
        "file_type": "pdf",
        "metadata": {},
        "schemes": {},
    }
    assert "document_class" not in legacy
    assert classify(legacy) == FULL_PORTFOLIO


# ---------------------------------------------------------------------------
# precedence / conflict rules
# ---------------------------------------------------------------------------

def test_factsheet_beats_generic_portfolio_token():
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\Factsheet_Portfolio_July_2026.pdf")
    assert classify(payload) == FACTSHEET_TOPN


def test_fortnightly_beats_generic_portfolio_token():
    payload = _excel_payload(
        r"data\raw\pdfs\DSP\2026\07\dsp-isin-debt-fortnightly-portfolio-as-on-15-jul-2026.xlsx")
    assert classify(payload) == FORTNIGHTLY


def test_top10_fund_brand_inside_monthly_portfolio_stays_full_portfolio():
    payload = _excel_payload(
        r"data\raw\pdfs\Motilal_Oswal_Mutual_Fund\2026\07\monthly-portfolio-july-2026.xlsx",
        scheme_name="Motilal Oswal BSE Top 10 Banks ETF")
    assert classify(payload) == FULL_PORTFOLIO


def test_top10_holdings_marker_in_scheme_name_is_factsheet_topn():
    payload = _excel_payload(
        r"data\raw\pdfs\Zerodha_Mutual_Fund\2026\07\Top 10 Holdings by Issuer as on July 31 2026.xlsx",
        scheme_name="Top 10 Holding of Schemes")
    assert classify(payload) == FACTSHEET_TOPN


# ---------------------------------------------------------------------------
# AI sidecar shape ({"file", "schemes": [{"name", ...}]})
# ---------------------------------------------------------------------------

def test_ai_sidecar_shape_classifies():
    sidecar = {
        "file": "ASK_Liquid_Fund_Fortnightly_Portfolio_Disclosures_15092026.xlsx",
        "schemes": [{"name": "ASK Liquid Fund", "n_holdings": 10}],
    }
    assert classify(sidecar) == FORTNIGHTLY
    sidecar["file"] = "Abakkus_MF_Factsheet_Mar_26_Final.pdf"
    assert classify(sidecar) == FACTSHEET_TOPN


# ---------------------------------------------------------------------------
# determinism + purity
# ---------------------------------------------------------------------------

def test_classify_is_deterministic():
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\CMLIQ_Fortnightly_Portfolio_Disclosure_July_15_2026.xlsx")
    first = classify(payload)
    for _ in range(3):
        assert classify(payload) == first


def test_classify_does_not_mutate_payload():
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\Factsheet - Jun 26.pdf")
    before = json.dumps(payload, sort_keys=True)
    classify(payload)
    assert json.dumps(payload, sort_keys=True) == before


def test_document_classes_constant():
    assert DOCUMENT_CLASSES == (
        "full_portfolio", "fortnightly", "factsheet_topn", "unknown")


# ---------------------------------------------------------------------------
# save_* stamping (additive; every existing key intact)
# ---------------------------------------------------------------------------

def test_save_excel_stamps_document_class(tmp_path):
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\Abakkus_MF_Factsheet_July_2026.pdf")
    save_excel_parsed_data(payload, tmp_path, "factsheet")
    saved = json.loads((tmp_path / "factsheet.json").read_text(encoding="utf-8"))
    assert saved["document_class"] == FACTSHEET_TOPN
    assert saved["source_file"] == payload["source_file"]
    assert saved["file_type"] == "excel"
    assert saved["schemes"]["Sheet1"]["fund_name"] == "Some Fund"


def test_save_html_stamps_document_class(tmp_path):
    payload = {
        "source_file": r"data\raw\pdfs\Kotak\2026\07\Monthly_Portfolio_Statement.html",
        "file_type": "html",
        "metadata": {"scheme_name": "Kotak Fund", "date": "July 31, 2026"},
        "schemes": {},
        "holdings": [],
        "sectors": [],
    }
    save_html_parsed_data(payload, tmp_path, "kotak_page")
    saved = json.loads((tmp_path / "kotak_page.json").read_text(encoding="utf-8"))
    assert saved["document_class"] == FULL_PORTFOLIO
    assert saved["metadata"]["scheme_name"] == "Kotak Fund"


def test_save_pdf_stamps_document_class(tmp_path):
    payload = {
        "source_file": r"data\raw\pdfs\AMC\2026\07\Abakkus_MF_Fortnightly_e1d762c5ac.pdf",
        "file_type": "pdf",
        "metadata": {},
        "equity_holdings": [],
        "debt_holdings": [],
        "sector_allocation": [],
        "top_holdings": [],
        "cash_allocation": None,
        "raw_tables": [],
    }
    save_parsed_data(payload, tmp_path, "fortnightly_doc")
    saved = json.loads((tmp_path / "fortnightly_doc.json").read_text(encoding="utf-8"))
    assert saved["document_class"] == FORTNIGHTLY
    assert saved["equity_holdings"] == []
    assert saved["cash_allocation"] is None


def test_save_zip_stamps_document_class(tmp_path):
    payload = {
        "source_file": r"data\raw\pdfs\AMC\2026\07\Monthly_Portfolio_July_2026.zip",
        "file_type": "zip",
        "metadata": {"archive_members": 3},
        "schemes": {},
        "amc_name": "AMC",
    }
    save_zip_parsed_data(payload, tmp_path, "portfolio_zip")
    saved = json.loads((tmp_path / "portfolio_zip.json").read_text(encoding="utf-8"))
    assert saved["document_class"] == FULL_PORTFOLIO
    assert saved["metadata"]["archive_members"] == 3
    assert saved["amc_name"] == "AMC"


def test_saved_json_reloads_and_classifies_consistently(tmp_path):
    payload = _excel_payload(
        r"data\raw\pdfs\AMC\2026\07\CMLIQ_Monthly_Portfolio_Disclosure_July_31_2026.xlsx")
    save_excel_parsed_data(payload, tmp_path, "monthly")
    saved = json.loads((tmp_path / "monthly.json").read_text(encoding="utf-8"))
    assert saved["document_class"] == FULL_PORTFOLIO
    assert classify(saved) == FULL_PORTFOLIO
