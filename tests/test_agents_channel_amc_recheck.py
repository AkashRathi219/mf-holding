"""[T33] ``amc_recheck`` channel tests (SPEC §11.3 channel 1 / AC-18).

Covers pure planning (ordered, non-empty, deterministic, forced-alternate
rule), the five AC-18 location categories, the injected-I/O execution walk
(full-portfolio Σ=97 closes; factsheet top-10 at Σ=45 does NOT close and is
recorded as a fallback observation; full-portfolio Σ=60 does not close),
taxonomy mapping of raised exceptions (never ``None`` on a failed run), the
WAF short-circuit (no further downloads, no evasion), escalation citizenship
(the ticket and the queue are never mutated) and tmp-path isolation: fakes
only, no network, no writes outside ``tmp_path``.
"""

from __future__ import annotations

import zipfile

import pytest

from src.agents.channels import CHANNEL_ORDER, ChannelResult
from src.agents.channels.amc_recheck import (
    CATEGORY_ARCHIVE,
    CATEGORY_DISCLOSURE_TABS,
    CATEGORY_FILENAME_VARIANTS,
    CATEGORY_MENU_PATHS,
    CATEGORY_ZIP_MEMBERS,
    CHANNEL,
    Attempt,
    Blocked,
    PARSE_STRATEGIES,
    enumerate_alternate_locations,
    pick_alternate_strategy,
    plan,
    run,
)
from src.agents.escalation import CHANNELS, STATUS_OPEN, Ticket
from src.agents.taxonomy import is_valid_code

WAF_CODE = "ERR_WAF_CLOUDFLARE_1015"
TOPN_CODE = "ERR_HOLDINGS_TOPN_ONLY"
INCOMPLETE_CODE = "ERR_HOLDINGS_INCOMPLETE_SUM"
PARSER_PARTIAL_CODE = "ERR_HOLDINGS_PARSER_PARTIAL"

TOP10_45 = (10.0, 9.0, 8.0, 5.0, 4.0, 3.0, 3.0, 1.5, 1.0, 0.5)


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


def _rows(*weights):
    return [{"instrument": f"H{i}", "weight_pct": w} for i, w in enumerate(weights, start=1)]


def _payload(scheme: str, weights) -> dict:
    return {
        "schemes": {
            scheme: {
                "fund_name": scheme,
                "date": "31 August 2026",
                "holdings": _rows(*weights),
            }
        }
    }


class FakeDownloader:
    """Injected downloader fake: records calls, returns canned paths."""

    def __init__(self, path: str | None = None, *, exc: BaseException | None = None,
                 blocked: Blocked | None = None, paths_per_call: int = 1):
        self.calls: list[Attempt] = []
        self.path = path
        self.exc = exc
        self.blocked = blocked
        self.paths_per_call = paths_per_call

    def __call__(self, attempt, ticket, session):
        self.calls.append(attempt)
        if self.exc is not None:
            raise self.exc
        if self.blocked is not None:
            return self.blocked
        if self.path is None:
            return []
        return [self.path] * self.paths_per_call


class FakeParser:
    """Injected parser fake: records (path, strategy) calls, returns payload."""

    def __init__(self, payload: dict | None = None, *, exc: BaseException | None = None):
        self.calls: list[tuple[str, str]] = []
        self.payload = payload
        self.exc = exc

    def __call__(self, path, strategy, session):
        self.calls.append((path, strategy))
        if self.exc is not None:
            raise self.exc
        return self.payload


# ---------------------------------------------------------------------------
# Planning: ordered, non-empty, deterministic, forced-alternate
# ---------------------------------------------------------------------------

def test_plan_returns_ordered_nonempty_attempts():
    attempts = plan(_ticket())
    assert isinstance(attempts, list) and attempts
    assert all(isinstance(a, Attempt) for a in attempts)
    locations = enumerate_alternate_locations("Test AMC")
    assert [a.location for a in attempts] == list(locations)
    assert all(a.parse_strategy in PARSE_STRATEGIES for a in attempts)
    assert all(a.description for a in attempts)


def test_plan_excludes_failed_strategy():
    failed = PARSE_STRATEGIES[0]
    attempts = plan(_ticket(), failed_strategy=failed)
    assert attempts
    assert all(a.parse_strategy != failed for a in attempts)
    assert attempts[0].parse_strategy == pick_alternate_strategy(failed)


def test_plan_is_deterministic():
    ticket = _ticket()
    assert plan(ticket, failed_strategy="regex_holdings") == plan(
        ticket, failed_strategy="regex_holdings")
    assert plan(ticket) == plan(ticket)


def test_pick_alternate_strategy_never_returns_failed():
    for strategy in PARSE_STRATEGIES:
        assert pick_alternate_strategy(strategy) != strategy
        assert pick_alternate_strategy(strategy) in PARSE_STRATEGIES
    assert pick_alternate_strategy(None) == PARSE_STRATEGIES[0]
    assert pick_alternate_strategy("not_a_strategy") == PARSE_STRATEGIES[0]


def test_enumerate_alternate_locations_covers_all_five_categories():
    locations = enumerate_alternate_locations("Test AMC")
    categories = {loc.category for loc in locations}
    assert categories == {
        CATEGORY_MENU_PATHS,
        CATEGORY_ARCHIVE,
        CATEGORY_DISCLOSURE_TABS,
        CATEGORY_FILENAME_VARIANTS,
        CATEGORY_ZIP_MEMBERS,
    }
    assert len(locations) == len({loc.key for loc in locations})


# ---------------------------------------------------------------------------
# Execution: full-portfolio Σ=97 closes the ticket
# ---------------------------------------------------------------------------

def test_run_full_portfolio_97pct_closes_ticket(tmp_path):
    path = str(tmp_path / "monthly_portfolio_Aug_2026.xlsx")
    downloader = FakeDownloader(path)
    parser = FakeParser(_payload("Test Fund", weights=(50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), downloader=downloader, parse=parser, session=None)
    assert isinstance(result, ChannelResult)
    assert result.channel == CHANNEL == "amc_recheck"
    assert result.success is True
    assert result.failure_code is None
    assert result.strategy_used in PARSE_STRATEGIES
    assert result.new_document_paths == [path]
    assert "full_portfolio" in result.reason
    assert "97.00%" in result.reason
    assert parser.calls[0][1] == result.strategy_used


# ---------------------------------------------------------------------------
# Execution: factsheet top-10 (Σ=45) does NOT close the ticket
# ---------------------------------------------------------------------------

def test_run_factsheet_top10_does_not_close(tmp_path):
    path = str(tmp_path / "factsheet_Aug_2026.pdf")
    downloader = FakeDownloader(path)
    parser = FakeParser(_payload("Test Fund", weights=TOP10_45))
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert result.failure_code == TOPN_CODE
    assert "factsheet_topn" in result.reason
    assert result.new_document_paths == [path]


# ---------------------------------------------------------------------------
# Execution: full-portfolio Σ=60 does not close
# ---------------------------------------------------------------------------

def test_run_full_portfolio_60pct_does_not_close(tmp_path):
    path = str(tmp_path / "monthly_portfolio_Aug_2026.xlsx")
    downloader = FakeDownloader(path)
    parser = FakeParser(_payload("Test Fund", weights=(30.0, 20.0, 6.0, 3.0, 1.0)))
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert result.failure_code == INCOMPLETE_CODE
    assert "60.00%" in result.reason


# ---------------------------------------------------------------------------
# Taxonomy mapping: raised exceptions carry a valid code, never None
# ---------------------------------------------------------------------------

def test_run_maps_parse_exception_to_taxonomy_code(tmp_path):
    path = str(tmp_path / "monthly_portfolio_Aug_2026.xlsx")
    downloader = FakeDownloader(path)
    parser = FakeParser(exc=zipfile.BadZipFile("not a zip"))
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert result.failure_code == "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    assert is_valid_code(result.failure_code)


def test_run_unclassified_exception_still_carries_code(tmp_path):
    path = str(tmp_path / "monthly_portfolio_Aug_2026.xlsx")
    downloader = FakeDownloader(path)
    parser = FakeParser(exc=ValueError("boom"))
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert result.failure_code == PARSER_PARTIAL_CODE
    assert is_valid_code(result.failure_code)


def test_run_download_exception_carries_code(tmp_path):
    downloader = FakeDownloader(exc=OSError("connection reset"))
    parser = FakeParser()
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert is_valid_code(result.failure_code)
    assert result.new_document_paths == []


# ---------------------------------------------------------------------------
# WAF short-circuit: no further downloads, no evasion
# ---------------------------------------------------------------------------

def test_run_waf_exception_short_circuits(tmp_path):
    downloader = FakeDownloader(exc=RuntimeError("error code: 1015: access denied"))
    parser = FakeParser()
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert result.failure_code == WAF_CODE
    assert is_valid_code(result.failure_code)
    assert len(downloader.calls) == 1
    assert parser.calls == []
    assert "without evading" in result.reason


def test_run_blocked_sentinel_short_circuits(tmp_path):
    downloader = FakeDownloader(blocked=Blocked())
    parser = FakeParser()
    result = run(_ticket(), downloader=downloader, parse=parser)
    assert result.success is False
    assert result.failure_code == WAF_CODE
    assert len(downloader.calls) == 1
    assert parser.calls == []


# ---------------------------------------------------------------------------
# Escalation citizenship + injected-I/O isolation
# ---------------------------------------------------------------------------

def test_run_never_mutates_ticket_or_queue(tmp_path):
    ticket = _ticket(channels_tried=[], attempts=0)
    downloader = FakeDownloader(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", weights=(50.0, 30.0, 10.0, 5.0, 2.0)))
    run(ticket, downloader=downloader, parse=parser)
    assert ticket.status == STATUS_OPEN
    assert ticket.channels_tried == []
    assert ticket.attempts == 0
    assert ticket.last_tried == ""


def test_run_honors_injected_locations(tmp_path):
    only = enumerate_alternate_locations("Test AMC")[:1]
    downloader = FakeDownloader(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", weights=(50.0, 30.0, 10.0, 5.0, 2.0)))
    result = run(_ticket(), locations=only, downloader=downloader, parse=parser)
    assert result.success is True
    assert len(downloader.calls) == 1


def test_run_requires_injected_callables():
    with pytest.raises(ValueError):
        run(_ticket())


def test_channel_order_mirrors_escalation_channels():
    assert CHANNEL_ORDER == CHANNELS
    assert CHANNEL in CHANNEL_ORDER
    assert CHANNEL_ORDER.index(CHANNEL) == 0


def test_no_writes_outside_tmp_path(tmp_path):
    downloader = FakeDownloader(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", weights=(50.0, 30.0, 10.0, 5.0, 2.0)))
    run(_ticket(), downloader=downloader, parse=parser)
    downloader = FakeDownloader(str(tmp_path / "factsheet_Aug_2026.pdf"))
    parser = FakeParser(_payload("Test Fund", weights=TOP10_45))
    run(_ticket(), downloader=downloader, parse=parser)
    assert list(tmp_path.iterdir()) == []
