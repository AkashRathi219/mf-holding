"""``src.agents.production`` tests - the REAL discover/download/parse bindings.

Covers the three factory contracts with fakes/monkeypatch only (zero network):
the registry fail-soft lookup (unknown AMC -> ``[]``), the adapter seam
forwarding (registry portfolio/factsheet URLs + target month/year, links
normalised to placement-ready dicts), the AC-6 limiter guard (a blocked host
gets zero adapter calls), the deliberate adapter-exception propagation (the
ladder's classification path), the zero-touch no-op download without an
``output_dir``, the ``DocumentDownloader`` delegation with per-link fallbacks
and the saved count, the unsupported-extension parse rule, the
``production_kwargs`` bundle (keys, dry-run mode, dispatcher wiring, extra
overrides), the §11.3 source-channel client binding (``build_source_client``'s
lazy ``httpx`` construction, rotated headers and same-origin ``Referer``
default, proven against a fake ``httpx`` module; ``source_channel_kwargs``
keys, explicit-client override, ``out_dir`` pass-through and its
``build_dispatcher`` splat compatibility) and the import-light guarantee
(stdlib-only module top level, proven by an AST scan AND a fresh-subprocess
``sys.modules`` check that also exercises both factories).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src.agents.production import (
    build_discover,
    build_download,
    build_parse,
    build_source_client,
    production_kwargs,
    source_channel_kwargs,
)
from src.amc_adapters.base import PDFLink

REPO_ROOT = Path(__file__).resolve().parents[1]
LIGHT_MODULES = ("httpx", "playwright", "pymupdf")

REGISTRY_ENTRY = {
    "mf_id": "999",
    "mf_name": "Test AMC Mutual Fund",
    "amc_monthly_portfolio_disclosure": "https://portfolio.example.test/disclosures",
    "amc_monthly_mf_factsheets": "https://factsheets.example.test/monthly",
}


def _registry_file(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "amc_registry.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


class FakeAdapter:
    """Adapter fake: records discover_documents kwargs, returns canned links or raises."""

    def __init__(self, links: list[PDFLink] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.links = list(links or [])
        self.error = error

    async def discover_documents(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return list(self.links)


class FakeLimiter:
    """RateLimiter stand-in: records is_blocked probes, returns a canned verdict."""

    def __init__(self, blocked: bool) -> None:
        self.blocked = blocked
        self.asked: list[str] = []

    def is_blocked(self, host: str) -> bool:
        self.asked.append(host)
        return self.blocked


class FakeDocumentDownloader:
    """DocumentDownloader fake: records download_file kwargs; skip.pdf URLs fail."""

    delay = 0.0

    def __init__(self, output_dir) -> None:
        self.output_dir = output_dir
        self.calls: list[dict] = []

    async def download_file(self, *, url, amc_name, year, month, filename):
        self.calls.append(
            {"url": url, "amc_name": amc_name, "year": year, "month": month, "filename": filename}
        )
        if url.endswith("skip.pdf"):
            return None
        return Path("saved") / filename


class FakeSourceClient:
    """Source-channel client fake: the .get/.post interface, zero network."""

    def get(self, url, **kw):
        return f"GET {url}"

    def post(self, url, **kw):
        return f"POST {url}"


def _install_fake_httpx(monkeypatch):
    """Swap in a fake ``httpx`` module; return (constructor kwargs, sent requests)."""
    constructed: list[dict] = []
    requests: list[dict] = []

    class FakeHttpxClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def request(self, method, url, **kwargs):
            requests.append({"method": method, "url": url, **kwargs})
            return f"{method} {url}"

    fake = types.ModuleType("httpx")
    fake.Client = FakeHttpxClient
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return constructed, requests


# ---------------------------------------------------------------------------
# build_discover
# ---------------------------------------------------------------------------


def test_build_discover_unknown_amc_returns_empty_without_raising(tmp_path):
    discover = build_discover(
        registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]), month=9, year=2026
    )

    assert discover("fast_http", "Not In Registry Mutual Fund") == []


def test_build_discover_calls_the_adapter_with_registry_urls_and_target(tmp_path, monkeypatch):
    adapter = FakeAdapter(
        links=[PDFLink(url="https://example.test/p.pdf", filename="p.pdf", month=9, year=2026)]
    )
    seen_names: list[str] = []

    def fake_get_adapter(name):
        seen_names.append(name)
        return adapter

    monkeypatch.setattr("src.amc_adapters.get_adapter", fake_get_adapter)
    discover = build_discover(
        registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]), month=9, year=2026
    )

    links = discover("fast_http", "Test AMC Mutual Fund")

    assert seen_names == ["Test AMC Mutual Fund"]
    assert adapter.calls == [
        {
            "portfolio_url": "https://portfolio.example.test/disclosures",
            "factsheet_url": "https://factsheets.example.test/monthly",
            "target_month": 9,
            "target_year": 2026,
        }
    ]
    assert links == [
        {
            "url": "https://example.test/p.pdf",
            "filename": "p.pdf",
            "month": 9,
            "year": 2026,
            "scheme_name": None,
            "document_type": "monthly_portfolio",
            "amc_name": "Test AMC Mutual Fund",
        }
    ]


def test_build_discover_defaults_to_a_calendar_month_when_both_omitted(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr("src.amc_adapters.get_adapter", lambda name: adapter)
    discover = build_discover(registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]))

    discover("fast_http", "Test AMC Mutual Fund")

    kwargs = adapter.calls[0]
    assert isinstance(kwargs["target_month"], int) and 1 <= kwargs["target_month"] <= 12
    assert isinstance(kwargs["target_year"], int) and kwargs["target_year"] > 2020


def test_build_discover_rejects_a_lone_month_or_year(tmp_path):
    with pytest.raises(ValueError):
        build_discover(registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]), month=9)
    with pytest.raises(ValueError):
        build_discover(registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]), year=2026)


def test_build_discover_returns_empty_when_the_limiter_blocks_the_host(tmp_path, monkeypatch):
    limiter = FakeLimiter(blocked=True)
    adapter = FakeAdapter(links=[PDFLink(url="https://example.test/p.pdf", filename="p.pdf")])
    monkeypatch.setattr("src.amc_adapters.get_adapter", lambda name: adapter)
    discover = build_discover(
        registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]),
        month=9,
        year=2026,
        limiter=limiter,
    )

    assert discover("fast_http", "Test AMC Mutual Fund") == []
    assert limiter.asked == ["Test AMC Mutual Fund"]
    assert adapter.calls == []


def test_build_discover_lets_adapter_exceptions_propagate(tmp_path, monkeypatch):
    adapter = FakeAdapter(error=RuntimeError("cloudflare 1015"))
    monkeypatch.setattr("src.amc_adapters.get_adapter", lambda name: adapter)
    discover = build_discover(
        registry_path=_registry_file(tmp_path, [REGISTRY_ENTRY]), month=9, year=2026
    )

    with pytest.raises(RuntimeError, match="cloudflare 1015"):
        discover("fast_http", "Test AMC Mutual Fund")


# ---------------------------------------------------------------------------
# build_download
# ---------------------------------------------------------------------------


def test_build_download_without_output_dir_is_a_zero_touch_noop(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("DocumentDownloader must never be constructed without an output_dir")

    monkeypatch.setattr("src.pdf_downloader.DocumentDownloader", _boom)
    download = build_download()

    assert download([]) == 0
    assert download(
        [{"url": "https://example.test/a.pdf", "filename": "a.pdf", "month": 9, "year": 2026}]
    ) == 0


def test_build_download_delegates_to_the_downloader_and_counts_successes(tmp_path, monkeypatch):
    instances: list[FakeDocumentDownloader] = []

    def fake_factory(output_dir):
        instance = FakeDocumentDownloader(output_dir)
        instances.append(instance)
        return instance

    monkeypatch.setattr("src.pdf_downloader.DocumentDownloader", fake_factory)
    download = build_download(output_dir=tmp_path, month=9, year=2026)

    count = download(
        [
            {
                "url": "https://amc.example.test/a.pdf",
                "filename": "a.pdf",
                "month": 9,
                "year": 2026,
                "amc_name": "Test AMC",
            },
            {"url": "https://amc.example.test/b.pdf", "filename": "b.pdf"},
            {
                "url": "https://amc.example.test/skip.pdf",
                "filename": "skip.pdf",
                "month": 9,
                "year": 2026,
                "amc_name": "Test AMC",
            },
        ]
    )

    assert count == 2
    assert len(instances) == 1
    downloader = instances[0]
    assert downloader.output_dir == tmp_path
    assert downloader.calls == [
        {
            "url": "https://amc.example.test/a.pdf",
            "amc_name": "Test AMC",
            "year": 2026,
            "month": 9,
            "filename": "a.pdf",
        },
        {
            "url": "https://amc.example.test/b.pdf",
            "amc_name": "amc.example.test",
            "year": 2026,
            "month": 9,
            "filename": "b.pdf",
        },
        {
            "url": "https://amc.example.test/skip.pdf",
            "amc_name": "Test AMC",
            "year": 2026,
            "month": 9,
            "filename": "skip.pdf",
        },
    ]


# ---------------------------------------------------------------------------
# build_parse
# ---------------------------------------------------------------------------


def test_build_parse_returns_none_for_unsupported_extensions():
    parse = build_parse()

    assert parse("data/workbook.xlsx") is None
    assert parse("data/bundle.zip") is None
    assert parse("data/table.csv") is None
    assert parse("data/no-extension") is None


# ---------------------------------------------------------------------------
# build_source_client / source_channel_kwargs (the §11.3 source-channel client)
# ---------------------------------------------------------------------------


def test_build_source_client_exposes_get_and_post():
    client = build_source_client()

    assert callable(client.get)
    assert callable(client.post)


def test_build_source_client_constructs_httpx_lazily_with_rotated_headers(monkeypatch):
    constructed, requests = _install_fake_httpx(monkeypatch)
    client = build_source_client(timeout=30.0, verify=False)

    assert constructed == []  # lazy: no client built (and httpx not imported) yet

    response = client.get("https://www.amfiindia.com/otherdata/scheme-wise-disclosure")

    assert response == "GET https://www.amfiindia.com/otherdata/scheme-wise-disclosure"
    assert len(constructed) == 1
    options = constructed[0]
    assert options["timeout"] == 30.0
    assert options["verify"] is False
    assert options["follow_redirects"] is True
    assert "User-Agent" in options["headers"]  # rotated by src.utils.get_random_headers
    assert requests == [
        {
            "method": "GET",
            "url": "https://www.amfiindia.com/otherdata/scheme-wise-disclosure",
            "headers": {"Referer": "https://www.amfiindia.com/"},
        }
    ]


def test_build_source_client_honours_a_supplied_referer_and_builds_once(monkeypatch):
    constructed, requests = _install_fake_httpx(monkeypatch)
    client = build_source_client()
    caller_headers = {
        "User-Agent": "CALLER-UA",
        "Referer": "https://www.amfiindia.com/otherdata/page",
    }

    client.post(
        "https://www.amfiindia.com/api/schemewisedisclosure-investment",
        headers=caller_headers,
        params={"MF_ID": "1", "strMonth": "01-Jul-2026"},
    )
    client.get("https://www.amfiindia.com/otherdata/scheme-wise-disclosure")

    assert len(constructed) == 1  # one underlying client, reused across requests
    assert requests[0]["method"] == "POST"
    assert requests[0]["headers"] == caller_headers  # an explicit Referer wins
    assert requests[0]["params"] == {"MF_ID": "1", "strMonth": "01-Jul-2026"}
    assert caller_headers == {  # the caller's dict is copied, never mutated
        "User-Agent": "CALLER-UA",
        "Referer": "https://www.amfiindia.com/otherdata/page",
    }
    assert requests[1]["headers"] == {"Referer": "https://www.amfiindia.com/"}


def test_source_channel_kwargs_contains_the_expected_keys():
    kwargs = source_channel_kwargs()

    assert set(kwargs) >= {"client", "out_dir"}
    assert kwargs["client"] is not None  # the skipped_no_client short-circuit fix
    assert callable(kwargs["client"].get) and callable(kwargs["client"].post)
    assert kwargs["out_dir"] is None  # the dispatcher falls back to channel defaults


def test_source_channel_kwargs_explicit_client_override_wins():
    fake = FakeSourceClient()

    assert source_channel_kwargs(client=fake)["client"] is fake


def test_source_channel_kwargs_out_dir_passes_through(tmp_path):
    assert source_channel_kwargs(out_dir=tmp_path)["out_dir"] == tmp_path
    assert source_channel_kwargs(out_dir="data/parsed/amfi")["out_dir"] == "data/parsed/amfi"


def test_source_channel_kwargs_splats_into_build_dispatcher():
    from src.agents.dispatch import build_dispatcher

    kwargs = source_channel_kwargs(client=FakeSourceClient(), out_dir="OUT-DIR", limiter=None)

    assert kwargs["limiter"] is None  # extra overrides travel with the bundle
    assert callable(build_dispatcher(**kwargs))


# ---------------------------------------------------------------------------
# production_kwargs
# ---------------------------------------------------------------------------


def test_production_kwargs_returns_the_expected_bundle_keys():
    bundle = production_kwargs()

    assert set(bundle) >= {"discover", "download", "parse", "dispatcher", "dry_run"}
    assert callable(bundle["discover"])
    assert callable(bundle["download"])
    assert callable(bundle["parse"])
    assert callable(bundle["dispatcher"])
    assert bundle["dry_run"] is False


def test_production_kwargs_dry_run_ships_no_download():
    bundle = production_kwargs(dry_run=True)

    assert bundle["download"] is None
    assert bundle["dry_run"] is True


def test_production_kwargs_wires_the_dispatcher_with_the_parse_and_downloader(monkeypatch):
    seen: dict = {}

    def fake_build_dispatcher(**kwargs):
        seen.update(kwargs)
        return "SENTINEL-DISPATCHER"

    monkeypatch.setattr("src.agents.dispatch.build_dispatcher", fake_build_dispatcher)

    bundle = production_kwargs(
        parse="PARSER",
        downloader="DOWNLOADER",
        provider="PROVIDER",
        fetcher="FETCHER",
    )

    assert bundle["dispatcher"] == "SENTINEL-DISPATCHER"
    assert seen["parse"] == "PARSER"
    assert seen["downloader"] == "DOWNLOADER"
    assert seen["provider"] == "PROVIDER"
    assert seen["fetcher"] == "FETCHER"


def test_production_kwargs_passes_extra_overrides_through():
    bundle = production_kwargs(journal="JOURNAL", register_path="REGISTER")

    assert bundle["journal"] == "JOURNAL"
    assert bundle["register_path"] == "REGISTER"


# ---------------------------------------------------------------------------
# Import-light guarantee
# ---------------------------------------------------------------------------


def test_module_top_level_imports_are_stdlib_only():
    source = (REPO_ROOT / "src" / "agents" / "production.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top_level.add(node.module.split(".")[0])

    assert top_level.isdisjoint(LIGHT_MODULES), sorted(top_level & set(LIGHT_MODULES))
    non_stdlib = sorted(m for m in top_level if m not in sys.stdlib_module_names)
    assert non_stdlib == [], f"non-stdlib top-level imports: {non_stdlib}"


def test_fresh_import_and_factories_pull_no_httpx_playwright_or_pymupdf():
    code = (
        "import sys\n"
        "import src.agents.production as production\n"
        "client = production.build_source_client()\n"
        "kwargs = production.source_channel_kwargs()\n"
        "assert callable(kwargs['client'].get) and callable(kwargs['client'].post)\n"
        "heavy = [m for m in ('httpx', 'playwright', 'pymupdf') "
        "if any(k == m or k.startswith(m + '.') for k in sys.modules)]\n"
        "assert not heavy, heavy\n"
        "print('import-light ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert "import-light ok" in proc.stdout
