"""[T23] ``amfi`` source-agent channel tests (SPEC §11.2/§11.3 channel 3, AC-17).

All fakes + tmp_path: zero network, zero writes outside ``tmp_path``.  Covers
the full-portfolio Σ=97 close (parsed record written under the tmp out_dir),
the exact ``webapp/db.py::_load_amfi_schemes`` on-disk shape, the
factsheet/top-N no-close rule, unrecognisable payloads, the
``skipped_no_client`` short-circuit, append-only ``write_parsed`` (never
overwrites), the FLAT write layout (the file lands directly under ``out_dir``
and is picked up by the loader's top-level ``glob("*.json")`` - the regression
that would catch a re-introduced nested ``<AMC>/<YYYY>/<MM>/`` layout),
taxonomy mapping of client exceptions (never ``None`` on a failed run) and the
``document_class`` stamp from ``src.document_class.classify``.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.channels import ChannelResult
from src.agents.escalation import STATUS_OPEN, Ticket
from src.agents.source_amfi import (
    CHANNEL,
    DISCLOSURE_API_PATH,
    DISCLOSURE_PAGE_PATH,
    PORTFOLIO_SOURCE_LABEL,
    PORTFOLIO_STRATEGY,
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

# Next.js page chunk carrying the MF directory pair the channel resolves the
# ticket's AMC with (the raw text still holds the \\" escapes _rsc_unescape
# undoes, mirroring amfiindia.com's self.__next_f.push payloads).
PAGE_TEXT = (
    'self.__next_f.push([1,"{\\"mf_id\\":\\"18\\",'
    '\\"mf_name\\":\\"Test AMC Mutual Fund\\"}"])'
)


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


def _raw_rows(scheme: str, weights) -> list[dict]:
    return [
        {
            "MF_ID": "18",
            "Scheme_ID": "106",
            "Scheme_Name": scheme,
            "ISIN": f"INE00{index}A01021",
            "Company_Name": f"Company {index}",
            "Security_Type": "Investment - Equities",
            "MarketValue": round(weight * 100.0, 2),
            "MarketValuePercentage": weight,
            "QuarterDate": "2026-07-01T00:00:00.000Z",
            "QuarterName": "Jul-Sep 2026",
        }
        for index, weight in enumerate(weights, start=1)
    ]


def _raw_payload(scheme: str, weights) -> dict:
    return {"status": "ok", "rows": _raw_rows(scheme, weights)}


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
    """Injected HTTP client fake: records GET calls, routes by URL fragment."""

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
        DISCLOSURE_PAGE_PATH: FakeResponse(text=PAGE_TEXT),
        DISCLOSURE_API_PATH: FakeResponse(json_data=payload),
    })


def _parsed_record() -> dict:
    record = parse_candidate(
        _raw_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)),
        source_file=PORTFOLIO_SOURCE_LABEL,
    )
    assert record is not None
    record = dict(record)
    record["amc"] = "Test AMC"
    return record


# ---------------------------------------------------------------------------
# 1 + 2 + 8: full-portfolio Σ=97 closes, writes the loader shape, stamps
# document_class
# ---------------------------------------------------------------------------

def test_run_full_portfolio_97pct_closes_and_writes(tmp_path):
    client = _client_for(_raw_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert isinstance(result, ChannelResult)
    assert result.channel == CHANNEL == "amfi"
    assert result.success is True
    assert result.failure_code is None
    assert result.strategy_used == PORTFOLIO_STRATEGY
    assert "97.00%" in result.reason
    assert len(client.calls) == 2
    written = Path(result.new_document_paths[0])
    assert written.exists()
    assert written.parent == tmp_path
    assert written.name.startswith("test_amc_2026-07_")
    assert not list(tmp_path.rglob("*.tmp"))


def test_written_json_matches_load_amfi_schemes_shape(tmp_path):
    client = _client_for(_raw_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is True
    doc = json.loads(Path(result.new_document_paths[0]).read_text(encoding="utf-8"))
    assert {"amc", "as_of", "schemes"} <= set(doc)
    assert doc["amc"] == "Test AMC"
    assert doc["as_of"] == "2026-07-01"
    assert set(doc["schemes"]) == {"Test Fund"}
    holdings = doc["schemes"]["Test Fund"]["holdings"]
    assert len(holdings) == 5
    assert set(holdings[0]) == {
        "company", "isin", "percent_nav", "market_value", "sector", "section"}
    assert holdings[0]["company"] == "Company 1"
    assert holdings[0]["isin"] == "INE001A01021"
    assert holdings[0]["percent_nav"] == 50.0


def test_written_record_stamps_document_class(tmp_path):
    client = _client_for(_raw_payload("Test Fund", (50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is True
    doc = json.loads(Path(result.new_document_paths[0]).read_text(encoding="utf-8"))
    assert "document_class" in doc
    assert doc["document_class"] in DOCUMENT_CLASSES
    assert doc["document_class"] != "factsheet_topn"
    assert doc["document_class"] == classify_document(
        doc, source_file=PORTFOLIO_SOURCE_LABEL)


# ---------------------------------------------------------------------------
# 3: a top-N/factsheet candidate does NOT close the ticket
# ---------------------------------------------------------------------------

def test_run_factsheet_topn_does_not_close(tmp_path):
    client = _client_for(_raw_payload("Test Fund Top 10 Holdings", TOP10_45))
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
# 6: write_parsed never overwrites an existing file
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


# ---------------------------------------------------------------------------
# 6b: FLAT write layout - the webapp/db.py::_load_amfi_schemes contract
# (the loader reads a top-level AMFI_DIR.glob("*.json"); a nested
# <AMC>/<YYYY>/<MM>/ tree would be invisible to the merge)
# ---------------------------------------------------------------------------

def test_write_parsed_flat_layout_loader_glob_sees_file(tmp_path):
    record = _parsed_record()
    path = write_parsed(record, out_dir=tmp_path)
    assert path.parent == tmp_path
    assert path.name.endswith(".json")
    assert path.name.startswith("test_amc_")
    assert path in list(tmp_path.glob("*.json"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_parsed_two_months_two_files_both_globbed(tmp_path):
    record = _parsed_record()
    july = write_parsed(record, out_dir=tmp_path)
    august = write_parsed(dict(record, as_of="2026-08-01"), out_dir=tmp_path)
    assert july != august
    assert july.parent == august.parent == tmp_path
    assert "2026-07" in july.name
    assert "2026-08" in august.name
    globbed = {p.name for p in tmp_path.glob("*.json")}
    assert {july.name, august.name} <= globbed


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
    assert path.name.startswith("test_amc_")
    assert path.name.endswith(".json")
    assert path in list(tmp_path.glob("*.json"))
    again = write_parsed(record, out_dir=tmp_path)
    assert again == path


# ---------------------------------------------------------------------------
# 7: a client exception maps to a valid taxonomy code, none escapes
# ---------------------------------------------------------------------------

def test_run_client_exception_maps_to_taxonomy_code(tmp_path):
    client = FakeClient(exc=OSError("connection reset by peer"))
    result = run(_ticket(), client=client, out_dir=tmp_path)
    assert result.success is False
    assert result.failure_code
    assert is_valid_code(result.failure_code)
    assert not any(tmp_path.rglob("*.json"))
