"""Fleet runner: the CLI entry point that invokes the per-AMC agent fleet (SPEC §8.1, PLAN T11, AC-1/AC-11).

Nothing runs the agent framework in production yet - this module is that
entry point. :func:`run_all` selects AMCs from ``config/amc_registry.json``
(57 entries), builds ONE shared set of collaborators - the rate limiter, the
escalation queue, the episode journal, the knowledge ``register_path`` and
the §11.3 dispatcher (:func:`src.agents.dispatch.build_dispatcher`) - and runs
one :class:`src.agents.agent.Agent` per AMC under a bounded worker pool, then
folds everything into a :class:`RunSummary`.

Concurrency contract (SPEC §8.1): at most ``max_agents`` AMC agents (default
5) are in flight at any moment, and AMCs must never block each other - a slow
AMC occupies one worker while the others keep progressing. The bound is a
``concurrent.futures.ThreadPoolExecutor`` over the synchronous
``Agent.run``; the asyncio semaphore in PLAN §3 maps to this worker bound at
the runner layer (the per-host 2-permit politeness cap lives in
``src.agents.rate_limit`` and is shared through the ONE limiter instance).

Graceful degradation (AC-1): a per-AMC failure is contained at two layers and
NEVER escapes :func:`run_all`. First, the runner wraps each agent's
``discover`` callable: a discovery crash is recorded once per AMC in
``RunSummary.errors`` and re-raised, so the ladder's own classification and
fall-through policy stay untouched (the episode still logs the classified
failure). Second, the worker around ``Agent`` construction + ``run()`` catches
anything that escapes the framework (journal I/O, wiring bugs) and records it
the same way. The run itself always completes: individual AMC failures are
reported, not fatal, and the CLI exits 0 on a completed run.

Dry-run contract: ``dry_run=True`` is forwarded to every agent - the
downloader is never invoked, while episodes are still written (a rehearsal
logs too). The CLI always configures a journal (``--episodes``, defaulting to
the SPEC location) so a ``--dry-run`` run leaves its episode trail.

Zero-network safety: with no ``discover`` injected, a default no-op callable
returning ``[]`` is used, so a bare CLI invocation walks the full loop - queue
fold, register plan, ladder, episode log, register fold - without a single
network byte.  The opt-in ``--production`` flag (``run_all(production=True)``)
wires :func:`src.agents.production.production_kwargs` instead - the REAL
adapter discover, the real document downloader and the real AI parse - while
``--dry-run`` keeps its meaning under it (real discovery, zero downloads,
episodes still written).  Without the flag the runner stays a safe rehearsal
harness.

Windows note: the shared register is persisted with atomic ``os.replace``
saves, which fail transiently on Windows while a peer agent holds the file
open for a concurrent ``knowledge.load`` read (the PLAN-phase load runs
outside any lock). :class:`_FleetAgent` serializes the UPDATE half's register
fold with a fleet-wide lock - so concurrent episodes can neither overwrite
each other's fold nor wipe the register through a swallowed read failure -
and retries the save with backoff, turning the sharing violation into a save.
The lock covers only the fold (~1ms), never the ladder, so fleet concurrency
is unaffected; a save that still fails after the bounded retry surfaces as
that AMC's ``errors`` entry (graceful degradation), never as a crashed run.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.agents import production as production_module
from src.agents.agent import Agent, AgentResult
from src.agents.dispatch import build_dispatcher
from src.agents.episodes import (
    DEFAULT_ROOT as DEFAULT_EPISODE_ROOT,
    OUTCOME_SUCCESS,
    EpisodeJournal,
)
from src.agents.escalation import (
    DEFAULT_MANUAL_CSV_PATH,
    DEFAULT_QUEUE_PATH,
    EscalationQueue,
)
from src.agents.rate_limit import DEFAULT_STATE_MF_ID, RateLimiter
from src.agents.state import AgentState
from src.agents.strategies import DiscoverFn

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = BASE_DIR / "config" / "amc_registry.json"
# Where --production downloads land (the repo's own download destination,
# same convention as src/amc_direct.py).
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "data" / "raw" / "pdfs"

DEFAULT_MAX_AGENTS = 5
SAVE_RETRY_ATTEMPTS = 5
SAVE_RETRY_DELAY_SECONDS = 0.05

DownloadFn = Callable[[list], object]

__all__ = [
    "DEFAULT_DOWNLOAD_DIR",
    "DEFAULT_MAX_AGENTS",
    "DEFAULT_REGISTRY_PATH",
    "DownloadFn",
    "RunSummary",
    "load_registry",
    "main",
    "run_all",
]


def load_registry(path: str | Path | None = None) -> list[dict]:
    """Read the AMC registry; never raises (a missing or corrupt file is ``[]``).

    Returns every JSON-list entry that carries a non-empty ``mf_name`` - the
    field every per-AMC agent is keyed on. A missing, unreadable, corrupt or
    wrong-shaped registry file degrades to an empty list (logged, never
    raised), so a broken config can never crash the fleet.
    """
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    try:
        text = registry_path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        logger.warning("runner: registry %s unreadable (%s); continuing with an empty registry", registry_path, exc)
        return []
    try:
        raw = json.loads(text)
    except ValueError as exc:
        logger.warning("runner: registry %s corrupt (%s); continuing with an empty registry", registry_path, exc)
        return []
    if not isinstance(raw, list):
        logger.warning("runner: registry %s is not a JSON list; continuing with an empty registry", registry_path)
        return []
    return [
        entry
        for entry in raw
        if isinstance(entry, dict) and str(entry.get("mf_name") or "").strip()
    ]


def _select_amcs(
    registry: list[dict], amc_names: Sequence[str] | None
) -> list[tuple[str, str]]:
    """``(mf_id, amc_name)`` pairs to run: every entry, or the filtered subset.

    ``amc_names`` filters case-insensitively by SUBSTRING on ``mf_name``
    (repeatable CLI ``--amc`` filters union); ``None`` (or an empty sequence)
    selects the whole registry in registry order.
    """
    entries = [
        (str(entry.get("mf_id") or ""), str(entry.get("mf_name") or ""))
        for entry in registry
    ]
    if amc_names is None:
        return entries
    if isinstance(amc_names, str):
        amc_names = [amc_names]
    needles = [str(name).strip().casefold() for name in amc_names if str(name).strip()]
    if not needles:
        return entries
    selected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in entries:
        if pair in seen:
            continue
        if any(needle in pair[1].casefold() for needle in needles):
            seen.add(pair)
            selected.append(pair)
    return selected


def _noop_discover(strategy: str, amc_name: str) -> list:
    """Default zero-network discover: the bare rehearsal finds nothing."""
    return []


def _production_target(month: object | None) -> dict:
    """``month``/``year`` kwargs for ``production_kwargs`` derived from the
    ``YYYY-MM`` reporting label; ``{}`` (the factory's own previous-calendar-
    month default) when the label is absent or unparseable."""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", str(month or "").strip())
    if not match:
        return {}
    year, mon = int(match.group(1)), int(match.group(2))
    if not 1 <= mon <= 12:
        return {}
    return {"month": mon, "year": year}


@dataclass
class RunSummary:
    """Fleet outcome for one :func:`run_all` invocation.

    ``total`` is the number of selected AMCs; ``succeeded`` counts agents whose
    episode ended ``SUCCESS`` and ``failed`` everything else (``PARTIAL`` /
    ``FAILED`` outcomes plus AMCs captured in ``errors``), so
    ``succeeded + failed == total`` always. ``results`` holds one
    :class:`AgentResult` per AMC whose run completed (registry order);
    ``errors`` holds at most one ``{"amc", "error"}`` entry per AMC - the
    first failure observed at the runner boundary (a discovery crash or an
    exception that escaped the agent framework). ``dry_run`` mirrors the
    requested mode.
    """

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    dry_run: bool = False
    results: list[AgentResult] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


class _ErrorSink:
    """Thread-safe, at-most-one-entry-per-AMC collector for fleet failures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self.errors: list[dict[str, str]] = []

    def record_once(self, amc: str, error: str) -> None:
        with self._lock:
            if amc in self._seen:
                return
            self._seen.add(amc)
            self.errors.append({"amc": amc, "error": error})


def _guarded_discover(amc_name: str, inner: DiscoverFn, sink: _ErrorSink) -> DiscoverFn:
    """Wrap one AMC's ``discover``: record the first crash, re-raise untouched.

    The record gives the fleet summary visibility of the most common
    production failure (a discovery crash) even though the strategy ladder
    swallows per-rung exceptions by design; the re-raise keeps the ladder's
    own classification, fall-through and block-stop policy authoritative.
    """

    def discover(strategy: str, amc: str) -> list:
        try:
            return inner(strategy, amc)
        except Exception as exc:
            sink.record_once(amc_name, f"{type(exc).__name__}: {exc}")
            raise

    return discover


class _FleetAgent(Agent):
    """Agent as deployed by the fleet runner: serialized, Windows-safe register fold.

    The runner runs up to ``max_agents`` agents concurrently against ONE
    shared knowledge register, which adds two fleet-level hazards the agent
    framework itself never sees:

    * ``knowledge.save`` replaces the register file atomically; on Windows
      that replace fails with ``PermissionError`` while a peer holds the file
      open for a concurrent read (the PLAN-phase load runs outside any lock).
      A bounded retry with backoff turns the transient sharing violation into
      a save; an exhausted retry surfaces as that AMC's ``errors`` entry.
    * The UPDATE half's load -> fold -> save must not interleave with a
      peer's, or one episode's register fold is silently lost (and a read
      that lands mid-replace would be swallowed by ``knowledge.load`` as an
      empty register and saved back over the peer folds). The fleet-wide
      lock makes the fold atomic; it is held ~1ms per agent, never across
      the ladder, so fleet concurrency is unaffected.
    """

    def __init__(
        self,
        *args: object,
        register_lock: threading.Lock | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._register_lock = register_lock if register_lock is not None else threading.Lock()

    def _report(self, result: AgentResult) -> AgentResult:
        delay = SAVE_RETRY_DELAY_SECONDS
        for attempt in range(SAVE_RETRY_ATTEMPTS):
            try:
                with self._register_lock:
                    return super()._report(result)
            except PermissionError:
                if attempt == SAVE_RETRY_ATTEMPTS - 1:
                    raise
                logger.debug(
                    "runner: register save for %r hit a Windows sharing violation; retrying",
                    self.amc_name,
                )
                time.sleep(delay)
                delay *= 2
        return result


def run_all(
    amc_names: Sequence[str] | None = None,
    *,
    registry_path: str | Path | None = None,
    dry_run: bool = False,
    max_agents: int = DEFAULT_MAX_AGENTS,
    discover: DiscoverFn | None = None,
    download: DownloadFn | None = None,
    downloader: object = None,
    parse: object = None,
    provider: object = None,
    fetcher: object = None,
    limiter: RateLimiter | None = None,
    episode_root: str | Path | None = None,
    state_root: str | Path | None = None,
    queue_path: str | Path | None = None,
    manual_csv_path: str | Path | None = None,
    register_path: str | Path | None = None,
    journal: EpisodeJournal | None = None,
    rng: random.Random | None = None,
    now: datetime | str | None = None,
    month: object | None = None,
    production: bool = False,
) -> RunSummary:
    """Run one episode per selected AMC under the bounded fleet pool (AC-1, AC-11).

    Selection: every registry entry with an ``mf_name`` when ``amc_names`` is
    None, otherwise the case-insensitive substring matches (union across
    filters, registry order preserved). Shared collaborators are built ONCE:
    the ``limiter`` when injected, else a :class:`RateLimiter` whose breaker
    state lives under ``state_root`` (the limiter's own ``shared`` state id);
    one :class:`EscalationQueue` (``queue_path`` / ``manual_csv_path``,
    SPEC defaults when omitted); one journal - the injected ``journal`` wins,
    else :class:`EpisodeJournal` at ``episode_root`` when given, else no
    journal (library callers get no implicit writes; the CLI always passes a
    root); one shared ``register_path``; and one §11.3 dispatcher via
    :func:`build_dispatcher` (``downloader`` / ``parse`` / ``provider`` /
    ``fetcher`` / ``now`` forwarded). Each AMC gets its own durable
    :class:`AgentState(mf_id, root=state_root)`.

    ``discover`` defaults to a no-op returning ``[]`` (zero network);
    ``download`` is the agent's downloader and is never invoked in a
    ``dry_run``. ``month`` is forwarded to ``Agent.run`` as the reporting
    month label. ``max_agents`` bounds the worker pool (SPEC §8.1 default 5);
    a value below 1 raises ``ValueError`` from the pool constructor - a
    wiring bug, loud by design.

    ``parse`` is forwarded into the dispatcher so a fleet-level
    ``amc_recheck`` escalation can parse (and, wrapped for fraction-scale
    normalisation, the ``amfi`` / ``advisorkhoj`` source channels). With
    ``production=True`` the REAL bindings from
    :func:`src.agents.production.production_kwargs` are wired instead of the
    rehearsal defaults: the bundle's ``discover`` / ``download`` / ``parse``
    win whenever the corresponding argument was not injected explicitly
    (``dry_run`` travels with the bundle, so a production dry run ships no
    downloader at all), and the reporting ``month`` label doubles as the
    discovery/download target month when it parses as ``YYYY-MM``. The
    default stays ``production=False`` - the unchanged zero-network
    rehearsal.

    A per-AMC failure never escapes: discovery crashes are recorded by the
    discover guard, anything escaping ``Agent.run`` by the worker, and the
    summary is returned with ``errors`` populated. Returns the
    :class:`RunSummary`.
    """
    registry = load_registry(registry_path)
    selected = _select_amcs(registry, amc_names)

    if production:
        bundle = production_module.production_kwargs(
            dry_run=dry_run,
            registry_path=registry_path,
            output_dir=DEFAULT_DOWNLOAD_DIR,
            **_production_target(month),
        )
        if discover is None:
            discover = bundle["discover"]
        if download is None:
            download = bundle["download"]
        if parse is None:
            parse = bundle["parse"]

    journal_obj = (
        journal
        if journal is not None
        else (EpisodeJournal(episode_root) if episode_root is not None else None)
    )
    queue = EscalationQueue(
        queue_path=queue_path if queue_path is not None else DEFAULT_QUEUE_PATH,
        manual_csv_path=manual_csv_path if manual_csv_path is not None else DEFAULT_MANUAL_CSV_PATH,
    )
    if limiter is not None:
        shared_limiter = limiter
    elif state_root is not None:
        shared_limiter = RateLimiter(state=AgentState(DEFAULT_STATE_MF_ID, root=state_root))
    else:
        shared_limiter = RateLimiter()
    channel_runner = build_dispatcher(
        limiter=shared_limiter,
        downloader=downloader,
        parse=parse,
        provider=provider,
        fetcher=fetcher,
        now=now,
    )
    discover_fn = discover if discover is not None else _noop_discover
    register_lock = threading.Lock()
    sink = _ErrorSink()

    def run_one(entry: tuple[str, str]) -> AgentResult | None:
        mf_id, amc_name = entry
        try:
            state = (
                AgentState(mf_id, root=state_root)
                if state_root is not None
                else AgentState(mf_id)
            )
            agent = _FleetAgent(
                mf_id,
                amc_name,
                discover=_guarded_discover(amc_name, discover_fn, sink),
                download=download,
                journal=journal_obj,
                register_path=register_path,
                state=state,
                limiter=shared_limiter,
                rng=rng,
                queue=queue,
                channel_runner=channel_runner,
                dry_run=dry_run,
                now=now,
                register_lock=register_lock,
            )
            return agent.run(month=month)
        except Exception as exc:
            sink.record_once(amc_name, f"{type(exc).__name__}: {exc}")
            return None

    results: list[AgentResult] = []
    with ThreadPoolExecutor(max_workers=max_agents, thread_name_prefix="amc-agent") as pool:
        futures = [pool.submit(run_one, entry) for entry in selected]
        for future in futures:
            result = future.result()
            if isinstance(result, AgentResult):
                results.append(result)

    succeeded = sum(1 for result in results if result.outcome == OUTCOME_SUCCESS)
    return RunSummary(
        total=len(selected),
        succeeded=succeeded,
        failed=len(selected) - succeeded,
        dry_run=dry_run,
        results=results,
        errors=list(sink.errors),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="src.agents.runner",
        description=(
            "Run the per-AMC discovery agent fleet (SPEC §8.1). Without a "
            "production discover binding the run is a zero-network rehearsal: "
            "the full agent loop executes and episodes are logged, but no "
            "download happens in --dry-run and no discover finds anything."
        ),
    )
    parser.add_argument("--all", action="store_true", help="iterate every registry AMC")
    parser.add_argument(
        "--amc",
        action="append",
        default=[],
        metavar="NAME",
        help="substring filter on the AMC name (case-insensitive, repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="never download; episodes are still written",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help=(
            "wire the real production bindings (adapter discover, document "
            "downloader, AI parse) instead of the zero-network rehearsal; "
            "combine with --dry-run for real discovery without downloads"
        ),
    )
    parser.add_argument(
        "--max-agents",
        type=int,
        default=DEFAULT_MAX_AGENTS,
        metavar="N",
        help="maximum AMC agents running concurrently (default %(default)s)",
    )
    parser.add_argument(
        "--months",
        default=None,
        metavar="YYYY-MM",
        help="reporting month label forwarded to every agent run",
    )
    parser.add_argument("--register", default=None, metavar="PATH", help="knowledge register JSON path override")
    parser.add_argument("--queue", default=None, metavar="PATH", help="escalation queue JSONL path override")
    parser.add_argument("--episodes", default=None, metavar="PATH", help="episode journal root override")
    parser.add_argument("--state", default=None, metavar="PATH", help="agent state root override")
    parser.add_argument("--manual", default=None, metavar="PATH", help="manual-intervention CSV path override")
    return parser


def _print_summary(summary: RunSummary) -> None:
    mode = "dry-run" if summary.dry_run else "live"
    print(
        f"AMC agent fleet run complete ({mode}): total={summary.total} "
        f"succeeded={summary.succeeded} failed={summary.failed} errors={len(summary.errors)}"
    )
    for error in summary.errors:
        print(f"  ERROR {error['amc']}: {error['error']}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns 0 on a completed run (per-AMC errors are reported, not fatal)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.all and not args.amc:
        print("nothing to run: pass --all or --amc NAME (repeatable)", file=sys.stderr)
        return 2
    if args.max_agents < 1:
        parser.error(f"--max-agents must be >= 1, got {args.max_agents}")
    summary = run_all(
        amc_names=args.amc or None,
        dry_run=args.dry_run,
        max_agents=args.max_agents,
        month=args.months,
        register_path=args.register,
        queue_path=args.queue,
        manual_csv_path=args.manual,
        episode_root=args.episodes if args.episodes is not None else DEFAULT_EPISODE_ROOT,
        state_root=args.state,
        production=args.production,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
