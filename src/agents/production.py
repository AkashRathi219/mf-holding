"""Production bindings for the agent seams: the REAL discover / download / parse (SPEC §5, §8.1).

``src.agents.runner.run_all`` defaults ``discover`` to a no-op returning ``[]``, so a bare
fleet run is a zero-network rehearsal. This module is the missing link: factories that
build the REAL callables an :class:`src.agents.agent.Agent` (or ``run_all``) needs plus
the §11.3 source-channel HTTP client, wiring the existing machinery instead of
re-implementing any of it:

* :func:`build_discover` - ``discover(strategy, amc_name) -> list[dict]`` backed by
  ``config/amc_registry.json`` + ``src.amc_adapters.get_adapter(...).discover_documents(...)``.
* :func:`build_download` - ``download(links) -> int`` backed by
  ``src.pdf_downloader.DocumentDownloader`` (per-link month/year, the downloader's own
  politeness delay between requests).
* :func:`build_parse` - ``parse(path, parse_strategy=None, session=None) -> dict | None``
  backed by ``src.ai_extract.extract_pdf`` (the AI holdings tier; the free
  opencode-CLI-first transport ladder lives in ``src.agents.llm`` for text prompts, while
  document extraction goes through the configured OpenRouter-compatible provider).
* :func:`build_source_client` - the HTTP client the §11.3 source channels need: an
  object exposing ``.get(url, **kw)`` / ``.post(url, **kw)`` (the exact interface
  ``source_amfi.fetch_candidates`` / ``source_advisorkhoj.fetch_candidates`` already
  call), rotating realistic browser headers via ``src.utils.get_random_headers`` and
  adding a same-origin ``Referer`` when the caller supplies none. The underlying
  ``httpx.Client`` is constructed on the FIRST request, so building the client (or
  bundling it) is side-effect-free.
* :func:`source_channel_kwargs` - the ``{"client": ..., "out_dir": ..., ...}`` bundle
  ready to splat into ``src.agents.dispatch.build_dispatcher`` for scheduled runs.
* :func:`production_kwargs` - one bundle of the ``discover`` / ``download`` / ``parse`` /
  ``dispatcher`` kwargs, ready to wire into ``Agent(...)`` / ``run_all(...)``.

Scheduled-run wiring for the source channels (``amfi`` / ``advisorkhoj``)::

    from src.agents import dispatch
    from src.agents.production import source_channel_kwargs

    kwargs = source_channel_kwargs()  # lazy client, channel-default out_dirs
    runner = dispatch.build_dispatcher(**kwargs, limiter=limiter)

Without a wired client ``build_dispatcher`` forwards ``client=None`` and both source
channels short-circuit with ``skipped_no_client`` - this wiring is what turns them on.

Import-light: the module top level pulls ONLY the stdlib. Every import that drags httpx /
playwright / pymupdf / the whole adapter package happens lazily inside the factory
callables, so ``import src.agents.production`` stays cheap for every agent module that
never reaches production I/O (same convention as ``src.agents.llm``). The source-client
factory is lazier still: ``build_source_client`` imports nothing at all - ``httpx`` and
``src.utils.get_random_headers`` are imported inside its factory closure, which runs only
on the client wrapper's FIRST request.

Network honesty: ``discover`` performs real HTTP I/O against the AMC's own site (and, on
the HybridAdapter's fallback rung, real headless-browser I/O); ``download`` performs real
HTTP downloads into ``output_dir``; ``parse`` performs real LLM-provider I/O for PDFs;
``build_source_client``'s client performs real HTTP I/O against amfiindia.com /
advisorkhoj.com on every ``.get`` / ``.post`` - production/scheduled runs only. Tests
never reach any of it: everything heavy is injectable/overridable (registry path, target
month/year, limiter, and the adapter / downloader / AI-extract seams via their owning
modules) and the source channels take fakes via ``source_channel_kwargs(client=<fake>)``,
so tests run on fakes with zero network.

Exception policy (deliberate): ``discover`` does NOT swallow adapter exceptions. The
strategy ladder (``strategies.run_ladder``) maps a raised discover through
``taxonomy.classify_exception`` - a WAF/429/1015 block code stops the ladder immediately
(never hammer a blocking host) and any other code falls through to the next rung;
returning ``[]`` here would hide a block from that policy, so the exception propagates.
The runner's ``_guarded_discover`` records the same crash in ``RunSummary.errors`` and
re-raises, so fleet visibility and the ladder's classification both stay intact. Only two
cases fail soft with ``[]``: an AMC missing from the registry (nothing to probe) and a
limiter-blocked host (AC-6 - zero requests to a circuit-broken host; the ladder's own
pre-rung check uses the same ``amc_name`` breaker key, this guard is defense in depth for
standalone use of the factory).

Async bridge: both ``amc_adapters`` discovery and ``DocumentDownloader.download_file``
are async, while the agent seams are sync. Each factory callable runs ONE
``asyncio.run(...)`` per invocation (a fresh event loop per call), so it must be called
from a thread with NO running event loop - true for the fleet runner's worker threads and
plain scripts; calling one from inside a running loop raises ``RuntimeError`` (loud, by
design). The adapter seam is strategy-agnostic today: the ladder's rung name is logged
for observability, while ``discover_documents`` internally does fast-HTTP-first with a
Playwright fallback - the ladder in miniature.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DiscoverFn = Callable[[str, str], list]
DownloadFn = Callable[[list], int]
ParseFn = Callable[..., "dict | None"]

DEFAULT_PARSE_STRATEGY = "ai_extract_vision"

__all__ = [
    "DEFAULT_PARSE_STRATEGY",
    "DiscoverFn",
    "DownloadFn",
    "ParseFn",
    "build_discover",
    "build_download",
    "build_parse",
    "build_source_client",
    "production_kwargs",
    "source_channel_kwargs",
]


def _previous_calendar_month() -> tuple[int, int]:
    """The ``(month, year)`` pair disclosures are published for: the just-finished month."""
    now = datetime.now()
    month, year = now.month - 1, now.year
    if month == 0:
        month, year = 12, year - 1
    return month, year


def _resolve_target(month: object | None, year: object | None) -> tuple[int, int]:
    """Target ``(month, year)``: both given wins, both omitted -> previous calendar month.

    Exactly one of the two is a wiring bug and fails loudly (``ValueError``) at build
    time - the same loud-by-design convention as the runner's ``max_agents`` check.
    """
    if month is None and year is None:
        return _previous_calendar_month()
    if month is None or year is None:
        raise ValueError(
            "month and year must be given together (or both omitted for the previous calendar month)"
        )
    return int(month), int(year)


def _as_int(value: object) -> int | None:
    """Best-effort int coercion for link fields; ``None`` (or a bool) stays ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _link_field(link: object, name: str) -> object:
    """Read one field from a dict link or a ``PDFLink``-style object."""
    if isinstance(link, Mapping):
        return link.get(name)
    return getattr(link, name, None)


def _filename_from_url(url: str) -> str:
    """Fallback filename: the URL path's basename (query string excluded)."""
    return Path(urlsplit(url).path).name or "document.pdf"


def _host_from_url(url: str) -> str:
    """Fallback AMC key: the URL hostname (``None`` for a malformed URL becomes ``""``)."""
    return urlsplit(url).hostname or ""


def _link_to_dict(link: object, amc_name: str) -> dict:
    """Normalise one adapter link (``PDFLink``, dict or object) into a placement-ready dict.

    The result carries at least ``url`` / ``filename`` / ``month`` / ``year`` - everything
    :func:`build_download` needs to place the file - plus ``amc_name`` (the directory key
    ``DocumentDownloader.get_output_path`` lays out under) and the adapter's
    ``scheme_name`` / ``document_type`` when present.
    """
    if isinstance(link, Mapping):
        out = dict(link)
    else:
        out = {
            "url": getattr(link, "url", ""),
            "filename": getattr(link, "filename", ""),
            "month": getattr(link, "month", None),
            "year": getattr(link, "year", None),
            "scheme_name": getattr(link, "scheme_name", None),
            "document_type": getattr(link, "document_type", "monthly_portfolio"),
        }
    out.setdefault("amc_name", amc_name)
    return out


def build_discover(
    *,
    registry_path: str | Path | None = None,
    month: object | None = None,
    year: object | None = None,
    limiter: object | None = None,
) -> DiscoverFn:
    """Build the REAL ``discover(strategy, amc_name) -> list[dict]`` for the agent seams.

    NETWORK I/O: every call may fetch the AMC's own site over HTTP (and, on the
    HybridAdapter fallback, drive a headless browser). Tests must inject fakes.

    Behaviour:

    * The AMC is looked up in ``config/amc_registry.json`` (``runner.load_registry``
      semantics: a missing/corrupt file degrades to an empty registry). An unknown AMC
      fails soft with ``[]`` - nothing to probe is not a network failure.
    * With a ``limiter`` supplied, ``limiter.is_blocked(amc_name)`` is consulted BEFORE
      any adapter work and a blocked host returns ``[]`` (AC-6 - never hammer a
      circuit-broken host; the key matches the framework's breaker key).
    * Otherwise the AMC's adapter (``src.amc_adapters.get_adapter``) runs
      ``discover_documents(portfolio_url=..., factsheet_url=..., target_month=...,
      target_year=...)`` with the registry's two monthly-disclosure URLs and the target
      month/year (both args given, or the previous calendar month when both omitted).
    * Links are returned as placement-ready dicts (``url`` / ``filename`` / ``month`` /
      ``year`` / ``amc_name`` / ``scheme_name`` / ``document_type``).
    * Adapter exceptions PROPAGATE on purpose: ``strategies.run_ladder`` classifies them
      and stops on a WAF block - swallowing them here would break that designed path.
    """
    from src.agents.runner import DEFAULT_REGISTRY_PATH, load_registry

    entries = load_registry(registry_path if registry_path is not None else DEFAULT_REGISTRY_PATH)
    index = {str(entry.get("mf_name") or "").strip().casefold(): entry for entry in entries}
    target_month, target_year = _resolve_target(month, year)

    def discover(strategy: str, amc_name: str) -> list[dict]:
        logger.debug("production: discover rung %r for %r", strategy, amc_name)
        entry = index.get(str(amc_name or "").strip().casefold())
        if entry is None:
            logger.info("production: %r is not in the AMC registry; discover finds nothing", amc_name)
            return []
        if limiter is not None and limiter.is_blocked(str(amc_name)):
            logger.warning(
                "production: host %r is circuit-broken; discover dispatches zero requests (AC-6)",
                amc_name,
            )
            return []
        from src.amc_adapters import get_adapter

        adapter = get_adapter(str(entry.get("mf_name") or amc_name))
        links = asyncio.run(
            adapter.discover_documents(
                portfolio_url=str(entry.get("amc_monthly_portfolio_disclosure") or ""),
                factsheet_url=str(entry.get("amc_monthly_mf_factsheets") or ""),
                target_month=target_month,
                target_year=target_year,
            )
        )
        return [_link_to_dict(link, str(amc_name)) for link in links or []]

    return discover


async def _download_links(
    downloader: object, links: list, default_month: int, default_year: int
) -> int:
    """Download every link through the (async) ``DocumentDownloader``; return the count.

    Mirrors ``DocumentDownloader.download_all``'s pacing (the downloader's ``delay``
    between requests) while honouring each link's own month/year (falling back to the
    factory's target) and its own ``amc_name`` (falling back to the URL host). A link
    without a URL is skipped; a failed fetch (the downloader logs and returns ``None``)
    simply does not count.
    """
    count = 0
    for index, link in enumerate(links):
        delay = float(getattr(downloader, "delay", 0.0) or 0.0)
        if index and delay > 0:
            await asyncio.sleep(delay)
        url = str(_link_field(link, "url") or "").strip()
        if not url:
            logger.warning("production: download link %d carries no url; skipped", index)
            continue
        filename = str(_link_field(link, "filename") or "").strip() or _filename_from_url(url)
        amc_name = (
            str(_link_field(link, "amc_name") or "").strip() or _host_from_url(url) or "unknown"
        )
        link_month = _as_int(_link_field(link, "month"))
        link_year = _as_int(_link_field(link, "year"))
        saved = await downloader.download_file(
            url=url,
            amc_name=amc_name,
            year=link_year if link_year is not None else default_year,
            month=link_month if link_month is not None else default_month,
            filename=filename,
        )
        if saved is not None:
            count += 1
    return count


def build_download(
    *,
    output_dir: str | Path | None = None,
    year: object | None = None,
    month: object | None = None,
) -> DownloadFn:
    """Build the REAL ``download(links) -> int`` for the agent seams.

    NETWORK + FILESYSTEM I/O: every call fetches the link URLs over HTTP and writes the
    saved documents under ``output_dir`` (via ``DocumentDownloader``, which owns retries,
    content validation and the per-request politeness delay). Tests must inject fakes.

    With ``output_dir=None`` the factory returns a no-op that always returns 0 and never
    constructs a downloader - a dry run (or a caller that has not chosen a destination)
    touches nothing. With a real ``output_dir``, ``month`` / ``year`` (both given, or the
    previous calendar month when both omitted) are the fallback placement for links that
    carry no month/year of their own; the count is the number of documents actually
    saved. Filesystem-level wiring errors (e.g. an unwritable ``output_dir``) propagate
    for the agent's taxonomy to classify - per-fetch failures are the downloader's own
    logged, skipped failures.
    """
    if output_dir is None:
        def dry_download(links: list) -> int:
            return 0

        return dry_download

    target_month, target_year = _resolve_target(month, year)
    from src.pdf_downloader import DocumentDownloader

    downloader = DocumentDownloader(output_dir=Path(output_dir))

    def download(links: list) -> int:
        return asyncio.run(_download_links(downloader, links, target_month, target_year))

    return download


def _write_sidecar(sidecar: Path, record: dict) -> None:
    """Best-effort JSON sidecar for a parse record; a write failure never fails the parse."""
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "production: parse sidecar %s not written (%s); record returned anyway", sidecar, exc
        )


def build_parse(*, out_dir: str | Path | None = None) -> ParseFn:
    """Build the REAL ``parse(path, parse_strategy=None, session=None) -> dict | None``.

    NETWORK I/O for PDFs: extraction runs through ``src.ai_extract.extract_pdf`` (the AI
    holdings tier - it renders pages and talks to the configured OpenRouter-compatible
    provider; ``pymupdf`` is imported inside that call chain). Tests must inject fakes.

    Contract (matches the §11.3 channel seam, ``amc_recheck.run``'s ``parse``):

    * ``.pdf`` (case-insensitive) -> a normalised record in a repo-recognised parsed
      shape: ``{"source", "file_type", "parse_strategy", "holdings": [{company,
      percent_nav, isin}], "meta"}`` - the flat ``holdings`` bucket
      ``extract_scheme_holdings`` reads. ``parse_strategy`` is recorded for observability
      (default ``ai_extract_vision``); ``session`` is accepted for channel-contract
      uniformity and ignored (``ai_extract`` manages its own HTTP client).
    * Anything else -> ``None`` (unsupported here, never a raise): the XLSX/CSV/ZIP
      documents belong to the workbook/zip parsers, not the AI tier.
    * A PDF that cannot be extracted RAISES (``ai_extract.ExtractError`` and friends) -
      the channels classify parser failures through the taxonomy.
    * With ``out_dir`` given, the record is also persisted as
      ``<out_dir>/<stem>.parse.json`` (best-effort: a write failure is logged, the record
      is still returned).
    """
    sidecar_dir = Path(out_dir) if out_dir is not None else None

    def parse(path: object, parse_strategy: str | None = None, session: object = None) -> "dict | None":
        source = Path(str(path))
        if source.suffix.lower() != ".pdf":
            logger.info(
                "production: parse: unsupported document type %r for %s (only .pdf is handled here)",
                source.suffix,
                source.name,
            )
            return None
        from src import ai_extract

        rows, meta = ai_extract.extract_pdf(source)
        record = {
            "source": str(source),
            "file_type": "pdf",
            "parse_strategy": str(parse_strategy) if parse_strategy else DEFAULT_PARSE_STRATEGY,
            "holdings": list(rows),
            "meta": dict(meta),
        }
        if sidecar_dir is not None:
            _write_sidecar(sidecar_dir / f"{source.stem}.parse.json", record)
        return record

    return parse


def _origin_referer(url: str) -> str:
    """The URL's ``scheme://host/`` origin (``""`` when the URL has neither)."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/"


def _with_origin_referer(url: str, headers: object) -> object:
    """The caller's headers plus a same-origin ``Referer`` when none was provided.

    The probe is case-insensitive and the caller's mapping is copied, never mutated
    (callers may reuse one headers dict across requests).
    """
    if headers is not None and not isinstance(headers, Mapping):
        return headers
    merged = dict(headers or {})
    if any(str(key).casefold() == "referer" for key in merged):
        return merged
    origin = _origin_referer(url)
    if origin:
        merged["Referer"] = origin
    return merged


class _SourceHTTPClient:
    """The lazily-constructed client behind :func:`build_source_client`.

    ``factory`` builds the real transport (an ``httpx.Client``) on the FIRST request -
    never at construction - under a lock, so the fleet's worker threads can share one
    client. ``.get`` / ``.post`` forward every keyword (``params``, ``headers``,
    ``json``, ...) to the underlying client and add the same-origin ``Referer`` the
    source channels' landing requests want.
    """

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._client: object | None = None
        self._lock = threading.Lock()

    def _http(self) -> object:
        with self._lock:
            if self._client is None:
                self._client = self._factory()
            return self._client

    def _request(self, method: str, url: str, kw: dict) -> object:
        kw["headers"] = _with_origin_referer(url, kw.get("headers"))
        return self._http().request(method, url, **kw)

    def get(self, url: str, **kw: object) -> object:
        return self._request("GET", str(url), kw)

    def post(self, url: str, **kw: object) -> object:
        return self._request("POST", str(url), kw)


def build_source_client(*, timeout: float = 60.0, verify: bool = True) -> object:
    """Build the HTTP client the ``amfi`` / ``advisorkhoj`` source channels expect.

    NETWORK I/O: the returned object performs REAL network I/O against AMFI /
    Advisorkhoj on every ``.get(url, **kw)`` / ``.post(url, **kw)`` call - it is for
    production / scheduled runs only. Tests and unconfigured runs must keep
    ``client=None`` (the channels then short-circuit with ``skipped_no_client``) or
    inject fakes via :func:`source_channel_kwargs`.

    The exposed interface is exactly what ``source_amfi.fetch_candidates`` /
    ``source_advisorkhoj.fetch_candidates`` already call: ``.get(url, **kw)`` /
    ``.post(url, **kw)`` returning responses carrying ``.text`` / ``.json()``.

    Construction is LAZY: this factory imports nothing beyond the stdlib and builds no
    client object - ``httpx`` and ``src.utils.get_random_headers`` are imported inside
    the ``_create`` closure below, which runs on the FIRST request. Merely creating the
    client (or :func:`source_channel_kwargs` bundling it) is therefore side-effect-free
    and stays import-light.

    Request behaviour: the underlying client starts from ``get_random_headers()`` (the
    same rotating realistic-browser header set ``src.pdf_downloader`` and the AMC
    adapters already send); every request gains a ``Referer`` derived from the target
    URL's own origin (``scheme://host/``) unless the caller already provided one
    (case-insensitive), so first-page fetches look like same-site navigation while the
    channels' own per-request headers win wherever they pass one. Redirects are
    followed (the disclosure hosts redirect between www/apex and http/https, matching
    ``src.amfi_otherdata._client``).

    ``timeout`` (default 60s) and ``verify`` (default True) are forwarded to the
    underlying ``httpx.Client``; amfiindia.com's TLS chain is MITM-sensitive in some
    environments - ``src.amfi_otherdata`` ships ``verify=False`` for it, so operators
    can pass ``verify=False`` here too when a scheduled run needs it.
    """

    def _create() -> object:
        import httpx
        from src.utils import get_random_headers

        return httpx.Client(
            headers=get_random_headers(),
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
        )

    return _SourceHTTPClient(_create)


def source_channel_kwargs(
    *,
    timeout: float = 60.0,
    out_dir: object = None,
    client: object = None,
    **overrides: object,
) -> dict:
    """The ``client`` / ``out_dir`` kwargs the §11.3 source channels need.

    Returns ``{"client": ..., "out_dir": ..., **overrides}`` ready to splat into
    ``src.agents.dispatch.build_dispatcher`` - WITHOUT a real client the dispatcher
    forwards ``client=None`` and both source channels short-circuit with
    ``skipped_no_client`` forever. ``client=None`` (the default) builds the lazy
    production client (:func:`build_source_client` - nothing is imported or connected
    until its first request); an EXPLICIT ``client`` (a fake in tests) wins untouched.
    ``out_dir`` (default ``None``) passes through so the dispatcher falls back to each
    channel's own parsed-directory default; extra ``overrides`` (e.g. ``limiter=``)
    are merged in after the two standard keys, so the whole dict plus the caller's own
    seams splat in one go.

    Scheduled-run wiring (REAL network I/O - never in tests)::

        from src.agents import dispatch
        from src.agents.production import source_channel_kwargs

        kwargs = source_channel_kwargs(out_dir=Path("data/parsed"))
        runner = dispatch.build_dispatcher(**kwargs, limiter=limiter)
    """
    if client is None:
        client = build_source_client(timeout=timeout)
    kwargs: dict = {"client": client, "out_dir": out_dir}
    kwargs.update(overrides)
    return kwargs


def production_kwargs(*, dry_run: bool = False, **overrides: object) -> dict:
    """One bundle of the production kwargs for ``Agent(...)`` / ``run_all(...)``.

    Returns ``{"discover", "download", "parse", "dispatcher", "dry_run"}`` plus any extra
    ``overrides`` passed through untouched (e.g. ``journal=`` / ``register_path=``). The
    bundle is NOT blindly splattable into either entrypoint - ``Agent`` has no
    ``parse``/``dispatcher`` params and ``run_all`` has no ``parse``/``dispatcher`` params
    either - so callers pick the keys:

    * ``Agent`` wiring (the full loop, escalation included)::

        bundle = production_kwargs(output_dir=Path("data/raw/pdfs"))
        agent = Agent(
            mf_id, amc_name,
            discover=bundle["discover"],
            download=bundle["download"],
            channel_runner=bundle["dispatcher"],  # build_dispatcher(downloader=..., parse=bundle["parse"])
            dry_run=bundle["dry_run"],
        )

    * Fleet wiring: ``run_all`` builds its OWN dispatcher and forwards only
      ``downloader`` / ``provider`` / ``fetcher`` (no ``parse`` kwarg today), so pass the
      pieces explicitly::

        run_all(
            amc_names,
            discover=bundle["discover"],
            download=bundle["download"],
            downloader=<channel downloader callable>,
            dry_run=bundle["dry_run"],
        )

    Overrides (all optional): ``registry_path`` / ``month`` / ``year`` / ``limiter`` for
    :func:`build_discover`; ``output_dir`` / ``download_month`` / ``download_year`` for
    :func:`build_download` (defaulting to the shared ``month`` / ``year``);
    ``parse_out_dir`` for :func:`build_parse`; ``downloader`` / ``provider`` / ``fetcher``
    forwarded to :func:`src.agents.dispatch.build_dispatcher` together with the bundle's
    ``parse`` and the shared ``limiter``. Prebuilt ``discover`` / ``download`` / ``parse``
    / ``dispatcher`` overrides win over the factories (fakes for tests).

    ``dry_run=True`` ships ``download=None`` (the agent never invokes the downloader in a
    dry run) and ``dry_run`` itself in the bundle, so the mode travels with the wiring.
    """
    limiter = overrides.pop("limiter", None)
    month = overrides.pop("month", None)
    year = overrides.pop("year", None)
    registry_path = overrides.pop("registry_path", None)
    output_dir = overrides.pop("output_dir", None)
    download_month = overrides.pop("download_month", month)
    download_year = overrides.pop("download_year", year)
    parse_out_dir = overrides.pop("parse_out_dir", None)
    channel_downloader = overrides.pop("downloader", None)
    provider = overrides.pop("provider", None)
    fetcher = overrides.pop("fetcher", None)
    discover = overrides.pop("discover", None)
    download = overrides.pop("download", None)
    parse = overrides.pop("parse", None)
    dispatcher = overrides.pop("dispatcher", None)

    if discover is None:
        discover = build_discover(
            registry_path=registry_path, month=month, year=year, limiter=limiter
        )
    if download is None:
        download = (
            None
            if dry_run
            else build_download(output_dir=output_dir, month=download_month, year=download_year)
        )
    if parse is None:
        parse = build_parse(out_dir=parse_out_dir)
    if dispatcher is None:
        from src.agents.dispatch import build_dispatcher

        dispatcher = build_dispatcher(
            limiter=limiter,
            downloader=channel_downloader,
            parse=parse,
            provider=provider,
            fetcher=fetcher,
        )

    bundle = {
        "discover": discover,
        "download": download,
        "parse": parse,
        "dispatcher": dispatcher,
        "dry_run": dry_run,
    }
    bundle.update(overrides)
    return bundle
