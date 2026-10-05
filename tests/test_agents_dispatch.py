"""``src.agents.dispatch`` tests - the production ``channel_runner`` binding (SPEC §11.3).

Covers the routing contract (``amc_recheck`` / ``web_search`` reach their real
channel modules, ``amfi`` / ``advisorkhoj`` reach their real source-agent
modules - all proven through injected recording fakes), the explicit
``manual_channel_is_a_human_step`` reporting for the human-step ``manual``
channel and the ``channel_not_implemented`` reporting for unknown names (valid
taxonomy code, never an exception), exception mapping through the taxonomy (a
raising channel never escapes the dispatcher), ``failed_strategy`` forwarding
(the AC-18 forced-alternate rule stays the channel's own rotation), the
limiter politeness guard (a blocked host gets zero channel calls and a blocked
``ChannelResult``), the fraction-scale remediation wrapper (a Σ≈1.0 payload
reaches T0 where the raw fractions would fail, and a percent-scale payload is
never double-scaled) and the CHANNEL_IMPLS / DEFAULT_CHANNELS_IMPLEMENTED
invariants.  All fakes, zero network; the only filesystem writes land in
``tmp_path`` (the real queue's enqueue and the T0 records written under
``tmp_path`` out_dirs).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from src.agents import source_advisorkhoj, source_amfi
from src.agents.channels import ChannelResult
from src.agents.channels.amc_recheck import PARSE_STRATEGIES
from src.agents.dispatch import (
    CHANNEL_IMPLS,
    DEFAULT_CHANNELS_IMPLEMENTED,
    WAF_CODE,
    build_dispatcher,
)
from src.agents.escalation import CHANNELS, EscalationQueue
from src.agents.taxonomy import is_valid_code

FAILED_STRATEGY = PARSE_STRATEGIES[0]


class RecordingDownloader:
    """amc_recheck downloader fake: records attempts, returns canned paths."""

    def __init__(self, paths: list[str] | None = None) -> None:
        self.calls: list[object] = []
        self.paths = list(paths or [])

    def __call__(self, attempt, ticket, session):
        self.calls.append(attempt)
        return list(self.paths)


class RecordingParser:
    """Channel parser fake: records (path, strategy) calls, returns the payload."""

    def __init__(self, payload: object = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.payload = payload

    def __call__(self, path, strategy, session):
        self.calls.append((str(path), str(strategy)))
        return self.payload


class RecordingProvider:
    """web_search provider fake: records queries, resolves zero candidates."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[str]:
        self.queries.append(query)
        return []


class RecordingFetcher:
    """web_search fetcher fake: records URLs; nothing should reach it here."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url, ticket, session):
        self.calls.append(str(url))
        return []


class FakeLimiter:
    """RateLimiter stand-in: records is_blocked probes, returns a canned verdict."""

    def __init__(self, blocked: bool) -> None:
        self.blocked = blocked
        self.asked: list[str] = []

    def is_blocked(self, host: str) -> bool:
        self.asked.append(host)
        return self.blocked


class RecordingSourceChannel:
    """source_amfi/source_advisorkhoj run() fake: records kwargs, canned result."""

    def __init__(self, channel: str, success: bool = True) -> None:
        self.channel = channel
        self.calls: list[tuple[object, dict]] = []
        self.result = ChannelResult(
            channel=channel,
            success=success,
            reason="fake close" if success else "fake failure",
            failure_code=None if success else "ERR_SCHEME_MISSING_IN_DB",
        )

    def __call__(self, ticket, **kwargs):
        self.calls.append((ticket, kwargs))
        return self.result


@pytest.fixture
def ticket(tmp_path):
    """One real OPEN T2 ticket, enqueued through the real queue into tmp_path."""
    queue = EscalationQueue(
        queue_path=tmp_path / "q.jsonl",
        manual_csv_path=tmp_path / "manual.csv",
    )
    enqueued = queue.enqueue("Test AMC", "Test Scheme", "2026-08", "T2", 60.0, "full_portfolio")
    assert enqueued is not None
    return enqueued


# ---------------------------------------------------------------------------
# Routing to the real channel modules
# ---------------------------------------------------------------------------


def test_amc_recheck_routes_to_the_real_channel_module(ticket):
    downloader = RecordingDownloader()
    parser = RecordingParser()
    result = build_dispatcher(downloader=downloader, parse=parser)("amc_recheck", ticket)

    assert isinstance(result, ChannelResult)
    assert downloader.calls, "amc_recheck.run was never reached"
    assert result.channel == "amc_recheck"
    assert result.success is False
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)


def test_web_search_routes_to_the_real_channel_module(ticket):
    provider = RecordingProvider()
    fetcher = RecordingFetcher()
    parser = RecordingParser()
    result = build_dispatcher(provider=provider, fetcher=fetcher, parse=parser)(
        "web_search", ticket
    )

    assert isinstance(result, ChannelResult)
    assert provider.queries, "web_search.run was never reached"
    assert fetcher.calls == []
    assert result.channel == "web_search"
    assert result.success is False
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)


# ---------------------------------------------------------------------------
# Routing to the real source-agent modules
# ---------------------------------------------------------------------------


def test_amfi_routes_to_the_real_source_module(ticket, monkeypatch):
    fake = RecordingSourceChannel("amfi")
    monkeypatch.setattr(source_amfi, "run", fake)
    client = object()
    out_dir = "some/out/dir"
    result = build_dispatcher(client=client, out_dir=out_dir)("amfi", ticket)

    assert result.success is True
    assert len(fake.calls) == 1
    called_ticket, kwargs = fake.calls[0]
    assert called_ticket is ticket
    assert kwargs["client"] is client
    assert kwargs["out_dir"] == out_dir
    assert callable(kwargs["parse"])
    assert "now" in kwargs


def test_advisorkhoj_routes_to_the_real_source_module(ticket, monkeypatch):
    fake = RecordingSourceChannel("advisorkhoj")
    monkeypatch.setattr(source_advisorkhoj, "run", fake)
    client = object()
    out_dir = "other/out/dir"
    result = build_dispatcher(client=client, out_dir=out_dir)("advisorkhoj", ticket)

    assert result.success is True
    assert len(fake.calls) == 1
    called_ticket, kwargs = fake.calls[0]
    assert called_ticket is ticket
    assert kwargs["client"] is client
    assert kwargs["out_dir"] == out_dir
    assert callable(kwargs["parse"])
    assert "now" in kwargs


def test_source_channel_out_dir_defaults_to_the_channel_directory(ticket, monkeypatch):
    amfi_fake = RecordingSourceChannel("amfi")
    ak_fake = RecordingSourceChannel("advisorkhoj")
    monkeypatch.setattr(source_amfi, "run", amfi_fake)
    monkeypatch.setattr(source_advisorkhoj, "run", ak_fake)
    dispatcher = build_dispatcher()

    dispatcher("amfi", ticket)
    dispatcher("advisorkhoj", ticket)

    assert amfi_fake.calls[0][1]["out_dir"] == source_amfi.AMFI_PARSED_DIR
    assert ak_fake.calls[0][1]["out_dir"] == source_advisorkhoj.AK_PARSED_DIR


def test_source_channel_parse_is_wrapped_with_scale_normalisation(ticket, monkeypatch):
    seen = []

    def fake_parse(candidate, *, source_file=""):
        seen.append((candidate, source_file))
        return {"schemes": {"F": {"holdings": [{"percent_nav": 0.5}, {"percent_nav": 0.5}]}}}

    fake = RecordingSourceChannel("amfi")
    monkeypatch.setattr(source_amfi, "run", fake)
    dispatcher = build_dispatcher(parse=fake_parse)

    dispatcher("amfi", ticket)
    wrapped = fake.calls[0][1]["parse"]
    record = wrapped({"payload": 1}, source_file="amfi:label")

    assert seen == [({"payload": 1}, "amfi:label")]
    holdings = record["schemes"]["F"]["holdings"]
    assert [row["percent_nav"] for row in holdings] == [50.0, 50.0]


# ---------------------------------------------------------------------------
# The manual channel: a human step, never a fetch
# ---------------------------------------------------------------------------


def test_manual_channel_reports_the_human_step(ticket):
    result = build_dispatcher()("manual", ticket)

    assert result.channel == "manual"
    assert result.success is False
    assert "manual_channel_is_a_human_step" in result.reason
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)


def test_unknown_channel_name_behaves_like_unimplemented(ticket):
    result = build_dispatcher()("unknown_channel", ticket)

    assert result.channel == "unknown_channel"
    assert result.success is False
    assert "channel_not_implemented" in result.reason
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)


# ---------------------------------------------------------------------------
# Fraction-scale remediation on the source-channel routes (AC-7)
# ---------------------------------------------------------------------------

FRACTION_WEIGHTS = (0.40, 0.30, 0.15, 0.07, 0.05)
PERCENT_WEIGHTS = (40.0, 30.0, 15.0, 7.0, 5.0)


def _amfi_fraction_payload(scheme: str, weights) -> dict:
    return {
        "status": "ok",
        "rows": [
            {
                "Scheme_Name": scheme,
                "ISIN": f"INE00{index}A01021",
                "Company_Name": f"Company {index}",
                "Security_Type": "Investment - Equities",
                "MarketValue": round(weight * 100.0, 2),
                "MarketValuePercentage": weight,
                "QuarterDate": "2026-07-01T00:00:00.000Z",
            }
            for index, weight in enumerate(weights, start=1)
        ],
    }


def _advisorkhoj_fraction_payload(scheme: str, weights) -> dict:
    return {
        "amc": "Test AMC Mutual Fund",
        "files": [
            {
                "file": (
                    "Test AMC Mutual Fund\\08-2026\\portfolio\\"
                    "Monthly_Portfolio_31_08_2026.xls"
                ),
                "status": "ok",
                "sheets": [
                    {
                        "sheet": "S1",
                        "scheme": scheme,
                        "date": "2026-08-31",
                        "status": "ok",
                        "plans": {
                            "All": {
                                "holdings": [
                                    {
                                        "name": f"Company {index}",
                                        "isin": f"INE00{index}A01021",
                                        "pct_nav": weight,
                                    }
                                    for index, weight in enumerate(weights, start=1)
                                ]
                            }
                        },
                    }
                ],
            }
        ],
    }


def test_fraction_scale_amfi_payload_is_normalised_to_t0(ticket, tmp_path, monkeypatch):
    payload = _amfi_fraction_payload(ticket.scheme, FRACTION_WEIGHTS)
    monkeypatch.setattr(source_amfi, "fetch_candidates", lambda ticket, **kw: [payload])
    dispatcher = build_dispatcher(client=object(), out_dir=tmp_path)

    result = dispatcher("amfi", ticket)

    assert result.success is True, result.reason
    assert result.new_document_paths, "the T0 record was not written"
    record = json.loads(
        Path(result.new_document_paths[0]).read_text(encoding="utf-8")
    )
    holdings = record["schemes"][ticket.scheme]["holdings"]
    assert round(sum(row["percent_nav"] for row in holdings), 6) == 97.0


def test_fraction_scale_advisorkhoj_payload_is_normalised_to_t0(
    ticket, tmp_path, monkeypatch
):
    payload = _advisorkhoj_fraction_payload(ticket.scheme, FRACTION_WEIGHTS)
    monkeypatch.setattr(
        source_advisorkhoj, "fetch_candidates", lambda ticket, **kw: [payload]
    )
    dispatcher = build_dispatcher(client=object(), out_dir=tmp_path)

    result = dispatcher("advisorkhoj", ticket)

    assert result.success is True, result.reason
    assert result.new_document_paths, "the T0 record was not written"
    record = json.loads(
        Path(result.new_document_paths[0]).read_text(encoding="utf-8")
    )
    holdings = record["files"][0]["sheets"][0]["plans"]["All"]["holdings"]
    assert round(sum(row["percent_nav"] for row in holdings), 6) == 97.0
    assert all(row["pct_nav"] == row["percent_nav"] for row in holdings)


def test_percent_scale_amfi_payload_is_never_double_scaled(
    ticket, tmp_path, monkeypatch
):
    payload = _amfi_fraction_payload(ticket.scheme, PERCENT_WEIGHTS)
    monkeypatch.setattr(source_amfi, "fetch_candidates", lambda ticket, **kw: [payload])
    dispatcher = build_dispatcher(client=object(), out_dir=tmp_path)

    result = dispatcher("amfi", ticket)

    assert result.success is True, result.reason
    record = json.loads(
        Path(result.new_document_paths[0]).read_text(encoding="utf-8")
    )
    holdings = record["schemes"][ticket.scheme]["holdings"]
    assert round(sum(row["percent_nav"] for row in holdings), 6) == 97.0


# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["amc_recheck", "web_search"])
def test_raising_channel_is_caught_and_mapped(ticket, channel):
    dispatchers = {
        "amc_recheck": build_dispatcher(),
        "web_search": build_dispatcher(provider=RecordingProvider()),
    }

    result = dispatchers[channel](channel, ticket)

    assert result.channel == channel
    assert result.success is False
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)


# ---------------------------------------------------------------------------
# failed_strategy forwarding (AC-18 forced-alternate rule)
# ---------------------------------------------------------------------------


def test_failed_strategy_is_forwarded_to_the_channel(ticket):
    downloader = RecordingDownloader(paths=["portfolio.pdf"])
    parser = RecordingParser()
    result = build_dispatcher(downloader=downloader, parse=parser)(
        "amc_recheck", ticket, failed_strategy=FAILED_STRATEGY
    )

    assert downloader.calls, "amc_recheck.run was never reached"
    assert parser.calls, "parse was never reached"
    strategies = [strategy for _, strategy in parser.calls]
    assert FAILED_STRATEGY not in strategies, "the failed parse strategy was re-run"
    assert set(strategies) <= set(PARSE_STRATEGIES)
    assert result.success is False


# ---------------------------------------------------------------------------
# Politeness: the limiter guard
# ---------------------------------------------------------------------------


def test_limiter_blocked_host_dispatches_zero_channel_calls(ticket):
    limiter = FakeLimiter(blocked=True)
    downloader = RecordingDownloader()
    parser = RecordingParser()
    provider = RecordingProvider()
    dispatcher = build_dispatcher(
        limiter=limiter,
        downloader=downloader,
        parse=parser,
        provider=provider,
        fetcher=RecordingFetcher(),
    )

    result = dispatcher("amc_recheck", ticket)

    assert limiter.asked == [ticket.amc]
    assert downloader.calls == []
    assert provider.queries == []
    assert result.channel == "amc_recheck"
    assert result.success is False
    assert result.failure_code == WAF_CODE


def test_limiter_clear_host_still_dispatches(ticket):
    limiter = FakeLimiter(blocked=False)
    downloader = RecordingDownloader()
    dispatcher = build_dispatcher(limiter=limiter, downloader=downloader, parse=RecordingParser())

    result = dispatcher("amc_recheck", ticket)

    assert limiter.asked == [ticket.amc]
    assert downloader.calls, "the politeness guard blocked a clear host"
    assert result.success is False
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)


# ---------------------------------------------------------------------------
# CHANNEL_IMPLS / DEFAULT_CHANNELS_IMPLEMENTED invariants
# ---------------------------------------------------------------------------


def test_channel_impls_agree_with_the_escalation_ladder():
    assert set(CHANNEL_IMPLS) <= set(CHANNELS)
    assert DEFAULT_CHANNELS_IMPLEMENTED == frozenset(CHANNEL_IMPLS)
    assert set(CHANNELS) - set(CHANNEL_IMPLS) == {"manual"}
    for name, path in CHANNEL_IMPLS.items():
        module = importlib.import_module(path)
        assert callable(getattr(module, "run", None))
        assert module.CHANNEL == name


def test_channel_impls_covers_every_fetch_channel():
    assert set(CHANNEL_IMPLS) == {"amc_recheck", "web_search", "amfi", "advisorkhoj"}
    assert DEFAULT_CHANNELS_IMPLEMENTED == frozenset(CHANNEL_IMPLS)
