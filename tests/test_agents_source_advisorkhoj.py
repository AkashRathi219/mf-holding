"""[T24] ``advisorkhoj`` source-agent channel tests (SPEC §11.2/§11.3 channel 4, AC-17).

All fakes + tmp_path: zero network, zero writes outside ``tmp_path``.  Covers
the full-portfolio Σ=97 close (parsed record written under the tmp out_dir),
the FLAT write layout the ``webapp/db.py::_load_advisorkhoj_schemes`` loader
glob contract requires (the file lands directly under ``out_dir`` and is
picked up by the top-level ``glob("*.json")`` - the regression that would
catch a re-introduced nested ``<AMC>/<YYYY>/<MM>/`` layout), the exact loader
shape (amc/files/sheets/scheme/date/plans plus the loader's holding keys with
``percent_nav`` mirrored from ``pct_nav``), consumption by the real
``_load_advisorkhoj_schemes`` loader, the factsheet/top-N no-close rule,
unrecognisable payloads, the ``skipped_no_client`` short-circuit, append-only
``write_parsed`` (never overwrites), taxonomy mapping of client exceptions
(never ``None`` on a failed run) and the ``document_class`` stamp from
``src.document_class.classify``.
"""

from __future__ import annotations

import json
from pathlib import Path

import webapp.db as webapp_db
from src.agents.channels import ChannelResult
from src.agents.escalation import STATUS_OPEN, Ticket
from src.agents.source_advisorkhoj import (
    CHANNEL,
    DISCLOSURE_CATEGORY_PATH,
    DOWNLOAD_CENTRE_PATH,
    PORTFOLIO_SOURCE_LABEL,
    PORTFOLIO_STRATEGY,
    SOURCE_HOST,
    fetch_candidates,
    parse_candidate,
    run,
    write_parsed,
)
from src.agents.taxonomy import is_valid_code
from src.document_class import DOCUMENT_CLASSES, classify as classify_document

TOPN_CODE = "ERR_HOLDINGS_TOPN_ONLY"
MISSING_CODE = "ERR_SCHEME_MISSING_IN_DB"

TOP10_45 = (10.0, 9.0, 8.0, 5.0, 4.0, 3.0, 3.0, 1.5, 1.0, 0.5)

# The corpus's own document identity (files[].file): a portfolio path classifies
# full_portfolio, a factsheet path classifies factsheet_topn - the same
# "inner or rel" provenance src/agents/integrity.py::_index_advisorkhoj uses.
PORTFOLIO_FILE = "Test AMC Mutual Fund\\08-2026\\portfolio\\Monthly_Portfolio_31_08_2026.xls"
FACTSHEET_FILE = "Test AMC Mutual Fund\\08-2026\\factsheet\\Factsheet_August_2026.pdf"

# Download-centre page chunks carrying the documented Advisorkhoj markup:
# the <select id="select_company"> AMC list and the blue_text disclosure links
# (archive/docs/ADVISORKHOJ_PLAN.md, verified structure).
CENTRE_HTML = (
    '<select id="select_company">'
    '<option value="">Select AMC</option>'
    '<option value="test-amc-mutual-fund">Test AMC Mutual Fund</option>'
    "</select>"
)
CATEGORY_HTML = (
    "<ul><li><a class=\"blue_text\" target=\"_blank\" "
    'href="/documents/Test_AMC_Monthly_Portfolio_August_2026.json">'
    "Monthly Portfolio Disclosure - August 2026</a></li></ul>"
)

# Fake-client route fragments, most specific first: the download-centre path is
# a prefix of the category path, so route order decides the match.
DOC_FRAGMENT = "Test_AMC_Monthly_Portfolio_August_2026"
CATEGORY_FRAGMENT = DISCLOSURE_CATEGORY_PATH.format(slug="test-amc-mutual-fund").lstrip("/")


def _ticket(**overrides) -> Ticket:
    fields = dict(
        queue_id="ESC-test",
        amc="Test AMC",
        scheme="Test Fund",
        month="2026-08",
        tier="T2",
        coverage_pct=60.0,
        document_class="full_portfolio",
        channels_tried=[],
        attempts=0,
        first_seen="2026-10-04T00:00:00+00:00",
        last_tried="",
        status=STATUS_OPEN,
    )
    fields.update(overrides)
    return Ticket(**fields)


def _holdings(weights) -> list[dict]:
    return [
        {
            "name": f"Company {index}",
            "isin": f"INE00{index}A01021",
            "quantity": 1000.0 * index,
            "value": round(weight * 100.0, 2),
            "pct_nav": weight,
            "rating": "Banks",
            "industry": "Banks",
            "yield": "7.2",
            "section": "Equity",
        }
        for index, weight in enumerate(weights, start=1)
    ]


def _ak_payload(scheme: str, weights, *, file_path: str = PORTFOLIO_FILE,
                date: str = "2026-08-31") -> dict:
    return {
        "amc": "Test AMC Mutual Fund",
        "files": [{
            "file": file_path,
            "status": "ok",
            "sheets": [{
                "sheet": "S1",
                "scheme": scheme,
                "date": date,
                "status": "ok",
                "plans": {"All": {"holdings": _holdings(weights)}},
            }],
        }],
    }


class FakeResponse:
    """Injected response fake exposing both ``.text`` and ``.json()``."""

    def __init__(self, text: str = "", json_data: object = None):
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeClient:
    """Injected HTTP client fake: records GET calls, routes by URL fragment
    (most specific fragment first - see the route-order note above)."""

    def __init__(self, routes: dict[str, FakeResponse] | None = None,
                 exc: BaseException | None = None):
        self.routes = routes or {}
        self.calls: list[str] = []
        self.exc = exc

    def get(self, url, **kw):
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp
        raise AssertionError(f"unexpected URL: {url}")


def _client_for(payload: object) -> FakeClient:
    return FakeClient({
        DOC_FRAGMENT: FakeResponse(json_data=payload),
        CATEGORY_FRAGMENT: FakeResponse(text=CATEGORY_HTML),
        DOWNLOAD_CENTRE_PATH: FakeResponse(text=CENTRE_HTML),
    })


def _parsed_record() -> dict:
    record = parse_candidate(
        _ak_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)),
        source_file=PORTFOLIO_SOURCE_LABEL,
    )
    assert record is not None
    return record


# ---------------------------------------------------------------------------
# 1 + 2 + 8 + 9: full-portfolio Σ=97 closes, writes the loader shape FLAT,
# stamps document_class
# ---------------------------------------------------------------------------

def test_run_full_portfolio_97pct_closes_and_writes(tmp_path):
    client = _client_for(_ak_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert isinstance(result, ChannelResult)
    assert result.channel == CHANNEL == "advisorkhoj"
    assert result.success is True
    assert result.failure_code is None
    assert result.strategy_used == PORTFOLIO_STRATEGY
    assert "97.00%" in result.reason
    assert len(client.calls) == 3
    written = Path(result.new_document_paths[0])
    assert written.exists()
    assert written.parent == tmp_path
    assert written.name.startswith("test_amc_mutual_fund_2026-08_")
    assert not list(tmp_path.rglob("*.tmp"))


def test_written_file_is_flat_and_loader_glob_sees_it(tmp_path):
    client = _client_for(_ak_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is True
    written = Path(result.new_document_paths[0])
    assert written.parent == tmp_path
    assert written in list(tmp_path.glob("*.json"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_written_json_matches_load_advisorkhoj_schemes_shape(tmp_path):
    client = _client_for(_ak_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is True
    doc = json.loads(Path(result.new_document_paths[0]).read_text(encoding="utf-8"))
    assert {"amc", "files"} <= set(doc)
    assert doc["amc"] == "Test AMC Mutual Fund"
    assert doc["as_of"] == "2026-08-31"
    sheets = doc["files"][0]["sheets"]
    assert len(sheets) == 1
    sheet = sheets[0]
    assert {"sheet", "scheme", "date", "status", "plans"} <= set(sheet)
    assert sheet["scheme"] == "Test Fund"
    assert sheet["date"] == "2026-08-31"
    holdings = sheet["plans"]["All"]["holdings"]
    assert len(holdings) == 5
    assert {
        "name", "isin", "quantity", "value", "pct_nav", "percent_nav",
        "rating", "industry", "yield", "section",
    } <= set(holdings[0])
    assert holdings[0]["name"] == "Company 1"
    assert holdings[0]["isin"] == "INE001A01021"
    assert holdings[0]["pct_nav"] == 50.0
    assert holdings[0]["percent_nav"] == 50.0


def test_written_file_is_consumed_by_load_advisorkhoj_schemes(tmp_path, monkeypatch):
    client = _client_for(_ak_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is True
    monkeypatch.setattr(webapp_db, "ADVISORKHOJ_DIR", tmp_path)
    yielded = list(webapp_db._load_advisorkhoj_schemes())
    assert yielded, "loader yielded nothing for the written record"
    amc, source, as_of, payload = yielded[0]
    assert amc == "Test AMC Mutual Fund"
    assert source == "advisorkhoj"
    assert as_of == "2026-08-31"
    assert payload["fund_name"] == "Test Fund"
    assert len(payload["holdings"]) == 5
    assert payload["holdings"][0]["company"] == "Company 1"
    assert payload["holdings"][0]["isin"] == "INE001A01021"
    assert payload["holdings"][0]["percent_nav"] == 50.0


def test_written_record_stamps_document_class(tmp_path):
    client = _client_for(_ak_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is True
    doc = json.loads(Path(result.new_document_paths[0]).read_text(encoding="utf-8"))
    assert "document_class" in doc
    assert doc["document_class"] in DOCUMENT_CLASSES
    assert doc["document_class"] == "full_portfolio"
    assert doc["document_class"] != "factsheet_topn"
    assert doc["document_class"] == classify_document(
        doc, source_file=doc["source_file"])


# ---------------------------------------------------------------------------
# 3: a top-N/factsheet candidate does NOT close the ticket
# ---------------------------------------------------------------------------

def test_run_factsheet_topn_does_not_close(tmp_path):
    client = _client_for(
        _ak_payload("Test Fund Top 10 Holdings", TOP10_45, file_path=FACTSHEET_FILE))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is False
    assert result.failure_code == TOPN_CODE
    assert is_valid_code(result.failure_code)
    assert "factsheet_topn" in result.reason
    assert "does NOT close" in result.reason
    assert not any(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------------------
# 4: an unrecognisable payload fails with a valid taxonomy code
# ---------------------------------------------------------------------------

def test_run_unrecognisable_payload_fails_with_taxonomy_code(tmp_path):
    client = _client_for({"message": "No data found."})
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is False
    assert result.failure_code == MISSING_CODE
    assert is_valid_code(result.failure_code)
    assert "not a recognisable portfolio disclosure" in result.reason
    assert not any(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------------------
# 5: no injected client -> skipped_no_client, no exception, no network
# ---------------------------------------------------------------------------

def test_run_without_client_skips_without_exception(tmp_path):
    result = run(_ticket(), out_dir=tmp_path)
    assert result.success is False
    assert "skipped_no_client" in result.reason
    assert is_valid_code(result.failure_code)
    assert result.new_document_paths == []
    assert fetch_candidates(_ticket()) == []
    assert not any(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------------------
# 6: an exception from the client maps to a valid taxonomy code
# ---------------------------------------------------------------------------

def test_run_client_exception_maps_to_taxonomy_code(tmp_path):
    client = FakeClient(exc=OSError("connection reset by peer"))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is False
    assert result.failure_code
    assert is_valid_code(result.failure_code)
    assert not any(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------------------
# 7: write_parsed never overwrites an existing file
# ---------------------------------------------------------------------------

def test_write_parsed_never_overwrites(tmp_path):
    record = _parsed_record()
    first = write_parsed(record, out_dir=tmp_path)
    original = first.read_text(encoding="utf-8")
    again = write_parsed(record, out_dir=tmp_path)
    assert again == first
    assert again.read_text(encoding="utf-8") == original
    other = dict(record, as_of="2026-07-31")
    second = write_parsed(other, out_dir=tmp_path)
    assert second != first
    assert first.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in first.parent.glob("*.json")) == sorted(
        [first.name, second.name])
    assert not list(first.parent.glob("*.tmp"))


def test_write_parsed_same_record_twice_no_clobber(tmp_path):
    record = _parsed_record()
    first = write_parsed(record, out_dir=tmp_path)
    original = first.read_text(encoding="utf-8")
    again = write_parsed(record, out_dir=tmp_path)
    assert again == first
    assert again.read_text(encoding="utf-8") == original
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_write_parsed_missing_as_of_falls_back_to_content_hash(tmp_path):
    record = _parsed_record()
    record.pop("as_of")
    path = write_parsed(record, out_dir=tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("test_amc_mutual_fund_")
    assert path.name.endswith(".json")
    assert path in list(tmp_path.glob("*.json"))
    again = write_parsed(record, out_dir=tmp_path)
    assert again == path


# ---------------------------------------------------------------------------
# parse_candidate: the nested corpus shape in, the loader record out
# ---------------------------------------------------------------------------

def test_parse_candidate_mirrors_pct_nav_and_rejects_unrecognisable():
    assert CHANNEL == "advisorkhoj"
    assert SOURCE_HOST == "advisorkhoj.com"
    record = parse_candidate(
        _ak_payload("Test Fund", (50.0, 30.0)), source_file=PORTFOLIO_SOURCE_LABEL)
    assert record is not None
    assert record["amc"] == "Test AMC Mutual Fund"
    assert record["as_of"] == "2026-08-31"
    assert record["source_file"] == PORTFOLIO_FILE
    rows = record["files"][0]["sheets"][0]["plans"]["All"]["holdings"]
    assert rows[0]["pct_nav"] == 50.0
    assert rows[0]["percent_nav"] == 50.0
    assert parse_candidate({"message": "No data found."}) is None
    assert parse_candidate("not a payload") is None
    assert parse_candidate({"amc": "X", "files": []}) is None
    assert parse_candidate(
        {"amc": "X", "files": [{"file": "f", "sheets": [{"scheme": "", "plans": {}}]}]}
    ) is None
    assert parse_candidate(
        {"amc": "X", "files": [{"file": "f", "sheets": [{"scheme": "S", "plans": {}}]}]}
    ) is None


# ---------------------------------------------------------------------------
# graceful nothing-found: the AMC is not in the download-centre's AMC list
# ---------------------------------------------------------------------------

def test_run_amc_not_listed_reports_missing(tmp_path):
    client = FakeClient({
        DOWNLOAD_CENTRE_PATH: FakeResponse(
            text='<select id="select_company">'
                 '<option value="other-amc">Other AMC</option></select>'),
    })
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is False
    assert result.failure_code == MISSING_CODE
    assert is_valid_code(result.failure_code)
    assert "nothing fabricated" in result.reason
    assert not any(tmp_path.rglob("*.json"))
