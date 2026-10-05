"""[T34] ``web_search`` channel tests (SPEC §11.3 channel 2 / AC-19, OQ-10).

The allow-list gate is the safety-critical property under test: official
AMC/AMFI/Advisorkhoj hosts (and their subdomains) pass; look-alikes
(``axismf.com.evil.example``, ``evilaxismf.com``, ``notaxismf.com``) and
every non-allow-listed host are rejected; and the injected fetcher is NEVER
called with a non-allowed URL - non-allowed candidates are recorded as
``skipped_untrusted_host`` with ZERO fetch calls.  Also covers the OQ-10
provider contract (no provider -> ``skipped_no_provider``; the no-network
default provider runs but finds nothing), the closing rule (full portfolio
Σ >= 95 closes; a factsheet top-10 does NOT), the blocked-host
short-circuit, taxonomy mapping of provider/parse exceptions (never ``None``
on a failed run) and tmp-path isolation: fakes only, zero real network, no
writes outside ``tmp_path``.
"""

from __future__ import annotations

import inspect
import json
import zipfile

import pytest

from src.agents.channels import CHANNEL_ORDER, ChannelResult
from src.agents.channels.web_search import (
    ADVISORKHOJ_HOST,
    AMFI_HOST,
    REGISTRY_PATH,
    REGISTRY_URL_FIELDS,
    Candidate,
    CHANNEL,
    CODE_MISSING,
    CODE_PARSER_PARTIAL,
    CODE_TOPN_ONLY,
    NullSearchProvider,
    allowlist_hosts,
    build_allowlist,
    host_of,
    is_allowed,
    normalize_host,
    plan,
    run,
)
from src.agents.escalation import CHANNELS, STATUS_OPEN, Ticket
from src.agents.taxonomy import is_valid_code

WAF_CODE = "ERR_WAF_CLOUDFLARE_1015"
TOPN_CODE = "ERR_HOLDINGS_TOPN_ONLY"

TOP10_45 = (10.0, 9.0, 8.0, 5.0, 4.0, 3.0, 3.0, 1.5, 1.0, 0.5)
FULL_97 = (50.0, 30.0, 10.0, 5.0, 2.0)

ALLOWED_URL = "https://www.axismf.com/downloads/monthly_portfolio_Aug_2026.xlsx"
UNTRUSTED_URLS = (
    "https://evil.example/monthly_portfolio_Aug_2026.xlsx",
    "https://axismf.com.evil.example/monthly_portfolio_Aug_2026.xlsx",
    "https://notaxismf.com/monthly_portfolio_Aug_2026.xlsx",
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


class FakeProvider:
    """Injected SearchProvider fake: records queries, returns canned URLs."""

    def __init__(self, urls: list[str] | None = None, *, exc: BaseException | None = None):
        self.calls: list[str] = []
        self.urls = list(urls or [])
        self.exc = exc

    def search(self, query, *, limit=10):
        self.calls.append(query)
        if self.exc is not None:
            raise self.exc
        return list(self.urls)


class FakeFetcher:
    """Injected fetcher fake: records EVERY url it is called with (AC-19 proof)."""

    def __init__(self, path: str | None = None, *, exc: BaseException | None = None,
                 blocked: object = None, paths_per_call: int = 1):
        self.calls: list[str] = []
        self.path = path
        self.exc = exc
        self.blocked = blocked
        self.paths_per_call = paths_per_call

    def __call__(self, url, ticket, session):
        self.calls.append(url)
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


class FakeGuard:
    """Injected is_blocked guard fake: records hosts, blocks the listed ones."""

    def __init__(self, blocked_hosts: set[str]):
        self.calls: list[str] = []
        self.blocked = blocked_hosts

    def __call__(self, host):
        self.calls.append(host)
        return host in self.blocked


# ---------------------------------------------------------------------------
# AC-19 allow-list: acceptance (official hosts + subdomains)
# ---------------------------------------------------------------------------

def test_is_allowed_accepts_official_hosts_and_subdomains():
    assert is_allowed("https://www.axismf.com/cms/product/factsheet") is True
    assert is_allowed("https://axismf.com/downloads/monthly-portfolio.pdf") is True
    assert is_allowed("https://downloads.axismf.com/monthly/Aug-2026.xlsx") is True
    assert is_allowed("HTTPS://WWW.AXISMF.COM/x.pdf") is True
    assert is_allowed("https://amfiindia.com/modules/PortDown.jsp") is True
    assert is_allowed("https://www.amfiindia.com/modules/PortDown.jsp") is True
    assert is_allowed("https://portal.amfiindia.com/x.pdf") is True
    assert is_allowed("https://www.advisorkhoj.com/mutual-funds/x.pdf") is True
    assert AMFI_HOST in allowlist_hosts()
    assert ADVISORKHOJ_HOST in allowlist_hosts()


def test_normalize_host_strips_www_and_case():
    assert normalize_host("WWW.AxisMF.COM.") == "axismf.com"
    assert normalize_host("  www.AmFiIndia.com  ") == "amfiindia.com"
    assert normalize_host("sub.axismf.com") == "sub.axismf.com"
    assert host_of("https://WWW.Example.COM/x") == "example.com"


def test_allowlist_matches_registry_derivation_exactly():
    with open(REGISTRY_PATH, encoding="utf-8-sig") as fh:
        registry = json.load(fh)
    assert len(registry) == 57
    derived: set[str] = set()
    for entry in registry:
        hosts = [
            host_of(entry.get(field))
            for field in REGISTRY_URL_FIELDS
            if str(entry.get(field) or "").strip()
        ]
        hosts = [h for h in hosts if h]
        for host in hosts:
            derived.add(host)
            assert host in allowlist_hosts(), entry["mf_name"]
            assert is_allowed(f"https://{host}/monthly-portfolio.pdf"), host
            assert is_allowed(f"https://www.{host}/monthly-portfolio.pdf"), host
    assert allowlist_hosts() == derived | {AMFI_HOST, ADVISORKHOJ_HOST}


def test_build_allowlist_derives_hosts_from_registry(tmp_path):
    registry = tmp_path / "amc_registry.json"
    registry.write_text(
        json.dumps([
            {
                "mf_name": "Fake AMC",
                "amc_monthly_portfolio_disclosure": "https://www.fakeamc.example/downloads",
            },
            {"mf_name": "Bare AMC", "scheme_wise": "https://downloads.bareamc.example/x.pdf"},
            {"mf_name": "Empty AMC"},
        ]),
        encoding="utf-8",
    )
    allowlist = build_allowlist(registry)
    assert "fakeamc.example" in allowlist
    assert "downloads.bareamc.example" in allowlist
    assert {AMFI_HOST, ADVISORKHOJ_HOST} <= allowlist
    assert is_allowed("https://www.fakeamc.example/x.pdf", allowlist=allowlist)
    assert is_allowed("https://sub.fakeamc.example/x.pdf", allowlist=allowlist)
    assert not is_allowed("https://notfakeamc.example/x.pdf", allowlist=allowlist)


# ---------------------------------------------------------------------------
# AC-19 allow-list: rejection (look-alikes, malformed, non-web schemes)
# ---------------------------------------------------------------------------

def test_is_allowed_rejects_look_alikes_and_malformed_urls():
    assert is_allowed("https://evil.example/x.pdf") is False
    assert is_allowed("https://axismf.com.evil.example/x.pdf") is False
    assert is_allowed("https://notaxismf.com/x.pdf") is False
    assert is_allowed("https://evilaxismf.com/x.pdf") is False
    assert is_allowed("http://axismf.com.evil.example/x.pdf") is False
    assert is_allowed("http://notaxismf.com/x.pdf") is False
    assert is_allowed("https://amfiindia.com.evil.example/x.pdf") is False
    assert is_allowed("https://advisorkhoj.com.evil.example/x.pdf") is False
    assert is_allowed("https://axismf.com@evil.example/x.pdf") is False
    assert is_allowed("https://evil.example/www.axismf.com/x.pdf") is False
    assert is_allowed("www.axismf.com/x.pdf") is False
    assert is_allowed("ftp://www.axismf.com/x.pdf") is False
    assert is_allowed("") is False
    assert is_allowed(None) is False


# ---------------------------------------------------------------------------
# AC-19 proof: the fetcher is NEVER called with a non-allowed URL
# ---------------------------------------------------------------------------

def test_fetcher_never_called_with_non_allow_listed_url(tmp_path):
    provider = FakeProvider(urls=[*UNTRUSTED_URLS, ALLOWED_URL])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert fetcher.calls, "allowed candidates must be fetched"
    for url in fetcher.calls:
        assert is_allowed(url), f"fetcher called with non-allowed URL {url}"
    for url in UNTRUSTED_URLS:
        assert url not in fetcher.calls
    assert result.success is True
    assert result.failure_code is None
    assert "skipped_untrusted_host" in result.reason


def test_untrusted_only_candidates_yield_zero_fetch_calls(tmp_path):
    provider = FakeProvider(urls=list(UNTRUSTED_URLS))
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert fetcher.calls == []
    assert parser.calls == []
    assert result.success is False
    assert "skipped_untrusted_host" in result.reason
    assert "evil.example" in result.reason
    assert result.failure_code == CODE_MISSING
    assert is_valid_code(result.failure_code)
    assert result.new_document_paths == []


# ---------------------------------------------------------------------------
# OQ-10 provider contract
# ---------------------------------------------------------------------------

def test_no_provider_skips_channel(tmp_path):
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), fetcher=fetcher, parse=parser)
    assert isinstance(result, ChannelResult)
    assert result.channel == CHANNEL == "web_search"
    assert result.success is False
    assert "skipped_no_provider" in result.reason
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)
    assert fetcher.calls == []
    assert parser.calls == []
    assert result.new_document_paths == []


def test_null_provider_runs_but_finds_nothing(tmp_path):
    provider = FakeProvider(urls=[])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert "skipped_no_provider" not in result.reason
    assert len(provider.calls) == len(plan(_ticket()))
    assert fetcher.calls == []
    assert is_valid_code(result.failure_code)


def test_null_search_provider_is_the_no_network_default():
    provider = NullSearchProvider()
    assert provider.search("any query", limit=5) == []
    assert provider.search("any query") == []


# ---------------------------------------------------------------------------
# Closing rule: full portfolio Σ >= 95 closes; factsheet top-10 does NOT
# ---------------------------------------------------------------------------

def test_allowed_full_portfolio_closes_ticket(tmp_path):
    path = str(tmp_path / "monthly_portfolio_Aug_2026.xlsx")
    provider = FakeProvider(urls=[ALLOWED_URL])
    fetcher = FakeFetcher(path)
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is True
    assert result.failure_code is None
    assert result.new_document_paths == [path]
    assert "full_portfolio" in result.reason
    assert "97.00%" in result.reason
    assert fetcher.calls == [ALLOWED_URL]
    assert parser.calls == [(path, result.strategy_used)]


def test_candidate_on_any_allow_listed_host_is_fetched(tmp_path):
    provider = FakeProvider(urls=["https://www.amfiindia.com/PortDown.jsp?mf=Test"])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert fetcher.calls == ["https://www.amfiindia.com/PortDown.jsp?mf=Test"]
    assert result.success is True


def test_factsheet_top10_does_not_close(tmp_path):
    path = str(tmp_path / "factsheet_Aug_2026.pdf")
    provider = FakeProvider(urls=[ALLOWED_URL])
    fetcher = FakeFetcher(path)
    parser = FakeParser(_payload("Test Fund", TOP10_45))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert result.failure_code == TOPN_CODE
    assert "factsheet_topn" in result.reason
    assert result.new_document_paths == [path]


# ---------------------------------------------------------------------------
# Blocked host short-circuit (no fetch, no evasion)
# ---------------------------------------------------------------------------

def test_blocked_host_short_circuits_before_fetch(tmp_path):
    provider = FakeProvider(urls=[ALLOWED_URL, "https://www.amfiindia.com/x.pdf"])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    guard = FakeGuard({"axismf.com"})
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser, is_blocked=guard)
    assert fetcher.calls == []
    assert parser.calls == []
    assert result.success is False
    assert result.failure_code == WAF_CODE
    assert is_valid_code(result.failure_code)
    assert "without evading" in result.reason
    assert guard.calls == ["axismf.com"]


# ---------------------------------------------------------------------------
# Taxonomy mapping: exceptions carry a valid code, never None
# ---------------------------------------------------------------------------

def test_provider_exception_maps_to_taxonomy_code(tmp_path):
    provider = FakeProvider(exc=RuntimeError("provider down"))
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)
    assert fetcher.calls == []


def test_fetcher_exception_maps_to_taxonomy_code(tmp_path):
    provider = FakeProvider(urls=[ALLOWED_URL])
    fetcher = FakeFetcher(exc=OSError("connection reset"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert is_valid_code(result.failure_code)
    assert result.new_document_paths == []


def test_parse_exception_maps_to_taxonomy_code(tmp_path):
    provider = FakeProvider(urls=[ALLOWED_URL])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(exc=zipfile.BadZipFile("not a zip"))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert result.failure_code == "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    assert is_valid_code(result.failure_code)


def test_unclassified_exception_still_carries_code(tmp_path):
    provider = FakeProvider(urls=[ALLOWED_URL])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(exc=ValueError("boom"))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert result.failure_code == CODE_PARSER_PARTIAL
    assert is_valid_code(result.failure_code)


def test_waf_exception_from_fetcher_short_circuits(tmp_path):
    provider = FakeProvider(urls=[ALLOWED_URL, "https://www.amfiindia.com/x.pdf"])
    fetcher = FakeFetcher(exc=RuntimeError("error code: 1015: access denied"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    result = run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert result.success is False
    assert result.failure_code == WAF_CODE
    assert len(fetcher.calls) == 1
    assert parser.calls == []


# ---------------------------------------------------------------------------
# Planning, escalation citizenship, isolation
# ---------------------------------------------------------------------------

def test_plan_proposes_deterministic_queries():
    first, second = plan(_ticket()), plan(_ticket())
    assert first == second
    assert first
    assert all(isinstance(c, Candidate) and c.query for c in first)
    assert any("2026-08" in c.query for c in first)
    assert any("August 2026" in c.query for c in first)


def test_run_never_mutates_ticket(tmp_path):
    ticket = _ticket(channels_tried=[], attempts=0)
    provider = FakeProvider(urls=[ALLOWED_URL])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    run(ticket, provider=provider, fetcher=fetcher, parse=parser)
    assert ticket.status == STATUS_OPEN
    assert ticket.channels_tried == []
    assert ticket.attempts == 0
    assert ticket.last_tried == ""


def test_run_requires_injected_callables():
    with pytest.raises(ValueError):
        run(_ticket(), provider=FakeProvider(urls=[ALLOWED_URL]))


def test_channel_order_web_search_is_second():
    assert CHANNEL_ORDER == CHANNELS
    assert CHANNEL in CHANNEL_ORDER
    assert CHANNEL_ORDER.index(CHANNEL) == 1


def test_no_writes_outside_tmp_path(tmp_path):
    provider = FakeProvider(urls=[ALLOWED_URL, *UNTRUSTED_URLS])
    fetcher = FakeFetcher(str(tmp_path / "monthly_portfolio_Aug_2026.xlsx"))
    parser = FakeParser(_payload("Test Fund", FULL_97))
    run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    provider = FakeProvider(urls=list(UNTRUSTED_URLS))
    fetcher = FakeFetcher(str(tmp_path / "factsheet_Aug_2026.pdf"))
    parser = FakeParser(_payload("Test Fund", TOP10_45))
    run(_ticket(), provider=provider, fetcher=fetcher, parse=parser)
    assert list(tmp_path.iterdir()) == []


def test_module_imports_no_network_libraries():
    import src.agents.channels.web_search as mod

    source = inspect.getsource(mod)
    for banned in (
        "import httpx",
        "import requests",
        "import socket",
        "urllib.request",
        "urlopen",
        "playwright",
        "import time",
        "time.sleep",
    ):
        assert banned not in source, banned
