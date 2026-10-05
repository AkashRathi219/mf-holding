"""Per-AMC discovery agent: the full SPEC §5 loop (steps 1-7).

One :class:`Agent` instance owns one AMC (``mf_id`` + ``amc_name``) and runs
the OBSERVE -> PLAN -> ACT -> OBSERVE RESULT half of the 7-step loop in
:meth:`Agent.run`, then the LOG -> UPDATE -> REPORT half through the
:meth:`Agent._log_episode` / :meth:`Agent._report` hooks - ``run()`` calls
both at the end of every episode, on every exit path, blocked or not.

Everything that could touch the network or real artifacts is INJECTED, so
tests run with fakes and zero network:

* ``discover(strategy, amc_name) -> list`` - the strategy ladder's real work,
  bound in production to the HybridAdapter / curl_cffi / Playwright-token
  machinery in ``src/amc_adapters``.
* ``download(links) -> int`` - the document downloader; returns the number of
  documents saved (a list of saved paths is also accepted and counted by
  length). NEVER called when ``dry_run`` is True or the ladder failed.
* ``register_path`` - the playbook register read through ``knowledge.load``
  (which never raises; a missing or corrupt file loads as an empty register).
  ``None`` plans against an empty register.
* ``journal`` - an :class:`src.agents.episodes.EpisodeJournal`; when one is
  configured every episode - dry runs included - is appended to its month
  shard, ``None`` disables the write (the in-memory record is still built so
  the UPDATE half can fold it).
* ``state`` - a :class:`src.agents.state.AgentState` (or any object with
  ``is_blocked(host)``). The circuit breaker is checked BEFORE the escalation
  phase and the ladder: an open window skips the escalation phase entirely
  (zero ``channel_runner`` dispatches) and short-circuits the whole ACT step
  with ``blocked=True`` and zero ``discover`` calls (AC-6: zero requests to a
  circuit-broken host).
* ``limiter`` - passed through to :func:`strategies.run_ladder`, which applies
  its own pre-rung breaker check with the same host key (``amc_name``).
* ``rng`` - a ``random.Random`` for the bandit's epsilon-greedy draws
  (``bandit.seeded_rng``); ``None`` falls back to the bandit's module default.
* ``queue`` - an :class:`src.agents.escalation.EscalationQueue`; with a
  ``channel_runner`` also configured, its OPEN tickets for this AMC are
  worked BEFORE the normal PLAN/ACT half (SPEC §11.3 consumption, AC-16).
* ``channel_runner`` - the injectable escalation dispatcher
  (``channel_runner(channel_name, ticket, *, failed_strategy=None) ->
  ChannelResult``); production binds it to the §11.3 channel modules in
  ``src/agents/channels``. ``None`` disables escalation work entirely.

The seven steps of one episode - 1-4 in :meth:`Agent.run`, 5-7 in the hooks:

1. OBSERVE: the site fingerprint ``{"cdn", "cms", "auth"}`` (empty strings
   until a probing half exists) and its ``bandit.fingerprint_hash``; both are
   kept on the instance (``self.fingerprint`` / ``self.fingerprint_hash``) so
   the LOG half can write them into the episode record.
2. PLAN: ``bandit.select_strategy(register, fingerprint, rng=self.rng)``.
3. ACT: ``strategies.run_ladder(amc_name, discover=self.discover, skip=(),
   limiter=self.limiter)`` - only after the breaker check described above.
4. OBSERVE RESULT: ``discovered_count`` is the ladder's link count,
   ``downloaded_count`` the downloader's count (always 0 in a dry run), and
   ``reward`` comes from :func:`compute_reward`.
5. LOG (:meth:`Agent._log_episode`): the §7 episode record is assembled from
   the result plus the stored fingerprint, validated with
   ``episodes.normalize_episode`` BEFORE anything is written (a malformed
   episode fails loudly, never silently), then appended to the journal when
   one is configured - a dry run logs too. The ``episode_id``
   (``episodes.new_episode_id``) is attached to ``AgentResult.episode_id``;
   without a journal nothing is written and ``None`` is returned.
6. UPDATE (:meth:`Agent._report`): the validated episode is folded into the
   playbook register with ``bandit.update_from_episode`` and persisted with
   ``knowledge.save`` when ``register_path`` is configured.
7. REPORT: the (hook-produced) :class:`AgentResult` is returned unchanged.

Outcome rule: SUCCESS when the ladder succeeded and (something was downloaded
or the run is a dry run); PARTIAL when the ladder succeeded but a non-dry run
downloaded nothing; FAILED when the ladder failed, was blocked, or the
downloader raised. ``failure_code`` is ``None`` exactly when the outcome is
SUCCESS; every non-SUCCESS outcome carries a code - the downloader's
exception classified through ``taxonomy.classify_exception``, else the
ladder's code, else the ladder's ``DEFAULT_FAILURE_CODE`` fallback - so the
result can always be folded into an episode record (the episodes schema
requires a code on every non-SUCCESS outcome).

Escalation consumption (SPEC §11.3, AC-16/AC-18): with BOTH ``queue`` and
``channel_runner`` configured and the host NOT inside its circuit-breaker
window, ``run()`` first dispatches this AMC's pending tickets - :meth:`Agent.pending_tickets` returns the queue fold's
``status == "OPEN"`` rows for this AMC only - each to ``next_channel(ticket)``,
the first §11.3 channel the ticket has not tried yet, so the fixed channel
order is the queue's own ``channels_tried`` accounting, never the agent's.
The runner's ``ChannelResult.success`` decides the ``record_attempt``
verdict: a success closes the ticket (CLOSED rows are no longer pending, so
it is never dispatched again), a failure is capped by the queue's own
``N_max`` parking, which the agent never re-implements. The agent passes
``failed_strategy=None`` - it keeps no record of a parse strategy that
produced an incomplete set (the ticket schema does not carry one) and must
never hand the channel its own ladder strategy; the forced-alternate rule
(AC-18) is the channel's own rotation (``pick_alternate_strategy`` never
repeats the failed parse). The counts land on
``AgentResult.escalations_worked`` / ``AgentResult.escalations_closed``;
when at least one ticket was worked, the run's §7 episode is stamped
``trigger="escalation"`` + ``channel=<last channel worked>`` (both stay
``None`` otherwise). A host inside its breaker window never reaches this
phase at all: the escalation guard runs before any dispatch and the skip is
logged (AC-6). The dry-run contract covers escalations too: a dry run
dispatches no channel and never calls ``record_attempt``, so the live queue
is left untouched (a rehearsal must never consume attempts or drive tickets
toward MANUAL) while the episode is still written. A raising runner fails
the run loudly before the normal loop starts (no episode is logged for it):
wiring bugs must be visible, never swallowed.

``month`` is accepted by :meth:`Agent.run` as the reporting month label; the
loop does not write it into the episode record (the §7 schema has no month
key and ``normalize_episode`` drops unknown keys) - the journal's
``<YYYY-MM>`` shard is derived from the episode timestamp instead. There are
no httpx or Playwright imports here: like the ladder, this is an
orchestration layer over injected callables.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sized
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.agents import bandit, episodes, knowledge, taxonomy
from src.agents.channels import ChannelResult
from src.agents.escalation import STATUS_OPEN, EscalationQueue, Ticket, next_channel
from src.agents.episodes import (
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    OUTCOMES,
    TRIGGER_ESCALATION,
)
from src.agents.strategies import (
    DEFAULT_FAILURE_CODE,
    DiscoverFn,
    WAF_1015_CODE,
    run_ladder,
)

logger = logging.getLogger(__name__)

DownloadFn = Callable[[list], int]
ChannelRunnerFn = Callable[..., ChannelResult]


def compute_reward(discovered: int, downloaded: int, success: bool) -> float:
    """SPEC §5 reward: ``1.0 * discovered + 2.0 * downloaded`` on success, else ``0.0``.

    Downloads weigh double: finding candidate links is progress, but only saved
    documents move the holdings pipeline, so the bandit's Q-update must value
    them more than bare discovery.
    """
    if not success:
        return 0.0
    return 1.0 * discovered + 2.0 * downloaded


@dataclass
class AgentResult:
    """One finished agent episode - the §7 record's fields, pre-LOG.

    ``outcome`` is one of ``SUCCESS | PARTIAL | FAILED``; ``failure_code`` is
    ``None`` exactly when ``outcome`` is SUCCESS and otherwise one of the 15
    stable taxonomy codes. ``episode_id`` stays ``None`` until the LOG half
    assigns it. ``escalations_worked`` / ``escalations_closed`` count the §11.3
    tickets this run dispatched / closed through the configured
    ``channel_runner`` (both 0 when no queue/runner is configured).
    """

    amc_name: str
    mf_id: str
    strategy: str
    outcome: str
    failure_code: str | None
    reward: float
    discovered_count: int
    downloaded_count: int
    dry_run: bool
    blocked: bool
    episode_id: str | None = None
    escalations_worked: int = 0
    escalations_closed: int = 0


def _downloaded_count(value: object) -> int:
    """Coerce the downloader's return value into a non-negative document count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return len(value) if isinstance(value, Sized) else 0
    return max(0, value)


class Agent:
    """One AMC's discovery agent; :meth:`run` plays the full 7-step loop.

    Steps 1-4 run directly in :meth:`run`; steps 5-7 run through the
    :meth:`_log_episode` / :meth:`_report` hooks that ``_finalize`` invokes on
    every exit path. The validated episode record of the most recent run is
    kept on :attr:`episode` between the two hooks. When a ``queue`` and a
    ``channel_runner`` are configured and the host is not circuit-broken,
    ``run`` first works this AMC's pending §11.3 escalation tickets (see
    :meth:`pending_tickets` / :meth:`_work_escalations`) and stamps the run's
    episode ``trigger="escalation"`` + ``channel=<last channel worked>``.
    """

    def __init__(
        self,
        mf_id: object,
        amc_name: str,
        *,
        discover: DiscoverFn,
        download: DownloadFn | None = None,
        journal: episodes.EpisodeJournal | None = None,
        register_path: str | Path | None = None,
        state: object | None = None,
        limiter: object | None = None,
        rng: random.Random | None = None,
        queue: EscalationQueue | None = None,
        channel_runner: ChannelRunnerFn | None = None,
        dry_run: bool = False,
        now: datetime | str | None = None,
    ) -> None:
        self.mf_id = str(mf_id)
        self.amc_name = amc_name
        self.discover = discover
        self.download = download
        self.journal = journal
        self.register_path = register_path
        self.state = state
        self.limiter = limiter
        self.rng = rng
        self.queue = queue
        self.channel_runner = channel_runner
        self.dry_run = dry_run
        self.now = now
        self.fingerprint: dict[str, str] = {"cdn": "", "cms": "", "auth": ""}
        self.fingerprint_hash = bandit.fingerprint_hash(self.fingerprint)
        self.episode: dict[str, object] | None = None
        self._escalation_channel: str | None = None

    def pending_tickets(self, amc_name: str) -> list[Ticket]:
        """This AMC's OPEN escalation tickets (SPEC §11.3), in queue fold order.

        Filters :meth:`src.agents.escalation.EscalationQueue.load` to
        ``ticket.amc == amc_name`` and ``status == "OPEN"``: CLOSED tickets are
        done and MANUAL ones are immutable to agents (§11.4), so a parked
        scheme-month never gets another automated attempt. Another AMC's
        tickets are never returned, and an agent without a queue has none.
        """
        if self.queue is None:
            return []
        return [
            ticket
            for ticket in self.queue.load().values()
            if ticket.amc == amc_name and ticket.status == STATUS_OPEN
        ]

    def run(self, month: object | None = None) -> AgentResult:
        """Run one full episode for this AMC.

        The circuit breaker is consulted FIRST: its verdict is computed once
        and reused for both the escalation guard and the ladder short-circuit.
        When the host is inside its block window, the §11.3 escalation phase
        (:meth:`_work_escalations`) is skipped entirely - zero
        ``channel_runner`` dispatches to the circuit-broken host (AC-6) - and
        the run returns a FAILED, blocked result with the WAF 1015 code. When
        a ``queue`` and a ``channel_runner`` are configured and the host is
        NOT blocked, the escalation phase runs before the normal PLAN/ACT
        work (the runner owns host safety at the wiring boundary too - the
        channels never evade a WAF and ``web_search`` accepts its own
        ``is_blocked`` guard). Steps 1-4 (OBSERVE -> PLAN -> ACT -> OBSERVE
        RESULT) then execute as before; an open window returns a FAILED,
        blocked result without a single ``discover`` call. Both exit paths
        converge on the ``_log_episode`` / ``_report`` hooks (steps 5-7),
        then the (hook-produced) :class:`AgentResult` is returned.
        """
        fingerprint = {"cdn": "", "cms": "", "auth": ""}
        self.fingerprint = fingerprint
        self.fingerprint_hash = bandit.fingerprint_hash(fingerprint)
        self._escalation_channel = None
        blocked = self.state is not None and self.state.is_blocked(self.amc_name)
        if blocked:
            logger.debug(
                "escalation phase skipped for %s: host is inside its circuit-breaker window",
                self.amc_name,
            )
            escalations_worked, escalations_closed = 0, 0
        else:
            escalations_worked, escalations_closed = self._work_escalations()
        register = knowledge.load(self.register_path) if self.register_path is not None else {}
        strategy = bandit.select_strategy(register, fingerprint, rng=self.rng)

        if blocked:
            return self._finalize(
                strategy,
                outcome=OUTCOME_FAILED,
                failure_code=WAF_1015_CODE,
                discovered_count=0,
                downloaded_count=0,
                blocked=True,
                escalations_worked=escalations_worked,
                escalations_closed=escalations_closed,
            )

        ladder = run_ladder(self.amc_name, discover=self.discover, skip=(), limiter=self.limiter)
        discovered_count = len(ladder.links)
        downloaded_count = 0
        download_error: Exception | None = None
        if ladder.success and not self.dry_run and self.download is not None:
            try:
                downloaded_count = _downloaded_count(self.download(ladder.links))
            except Exception as exc:
                download_error = exc
                downloaded_count = 0

        if not ladder.success or ladder.blocked or download_error is not None:
            outcome = OUTCOME_FAILED
        elif downloaded_count > 0 or self.dry_run:
            outcome = OUTCOME_SUCCESS
        else:
            outcome = OUTCOME_PARTIAL

        if outcome == OUTCOME_SUCCESS:
            failure_code = None
        else:
            classified = (
                taxonomy.classify_exception(download_error) if download_error is not None else None
            )
            failure_code = classified or ladder.failure_code or DEFAULT_FAILURE_CODE

        return self._finalize(
            strategy,
            outcome=outcome,
            failure_code=failure_code,
            discovered_count=discovered_count,
            downloaded_count=downloaded_count,
            blocked=ladder.blocked,
            escalations_worked=escalations_worked,
            escalations_closed=escalations_closed,
        )

    def _work_escalations(self) -> tuple[int, int]:
        """Dispatch this AMC's pending escalation tickets (SPEC §11.3, AC-16).

        ``run`` calls this only after its breaker check passes - a host inside
        its circuit-breaker window never reaches a ``channel_runner`` dispatch
        (AC-6). Runs only when BOTH ``queue`` and ``channel_runner`` are
        configured - either one alone leaves the normal loop untouched - and
        NEVER when ``dry_run`` is True: a dry run is side-effect-free for real
        work, so it dispatches no channel and never calls ``record_attempt``,
        returning ``(0, 0)`` with the live queue untouched (a rehearsal must
        never consume attempts or drive tickets toward MANUAL - only genuine
        data absence may). Each pending ticket
        (:meth:`pending_tickets`) is dispatched to ``next_channel(ticket)``,
        the first §11.3 channel the ticket has not tried yet, so the fixed
        channel order is the queue's own ``channels_tried`` accounting. The
        agent passes ``failed_strategy=None``: it keeps no record of a parse
        strategy that produced an incomplete set (the ticket schema does not
        carry one) and must never hand the channel its own ladder strategy -
        the forced-alternate rule (AC-18) is the channel's own rotation
        (``pick_alternate_strategy`` never repeats the failed parse).

        ``record_attempt`` is the ONLY queue mutation here: a successful
        ``ChannelResult`` records the attempt as closed, a failed one is
        capped by the queue's own ``N_max`` parking - never re-implemented in
        the agent. Returns ``(worked, closed)`` and leaves
        :attr:`_escalation_channel` on the last channel worked (``None`` when
        no ticket was worked) for the LOG half's trigger/channel stamp.
        """
        if self.queue is None or self.channel_runner is None:
            return 0, 0
        if self.dry_run:
            return 0, 0
        worked = 0
        closed = 0
        for ticket in self.pending_tickets(self.amc_name):
            channel = next_channel(ticket)
            result = self.channel_runner(channel, ticket, failed_strategy=None)
            self.queue.record_attempt(ticket, channel, result.success)
            worked += 1
            if result.success:
                closed += 1
            self._escalation_channel = channel
        return worked, closed

    def _finalize(
        self,
        strategy: str,
        *,
        outcome: str,
        failure_code: str | None,
        discovered_count: int,
        downloaded_count: int,
        blocked: bool,
        escalations_worked: int = 0,
        escalations_closed: int = 0,
    ) -> AgentResult:
        """Build the result, run the LOG hook, then the REPORT hook."""
        result = AgentResult(
            amc_name=self.amc_name,
            mf_id=self.mf_id,
            strategy=strategy,
            outcome=outcome,
            failure_code=failure_code,
            reward=compute_reward(discovered_count, downloaded_count, outcome == OUTCOME_SUCCESS),
            discovered_count=discovered_count,
            downloaded_count=downloaded_count,
            dry_run=self.dry_run,
            blocked=blocked,
            escalations_worked=escalations_worked,
            escalations_closed=escalations_closed,
        )
        episode_id = self._log_episode(result)
        if episode_id is not None:
            result.episode_id = episode_id
        return self._report(result)

    def _log_episode(self, result: AgentResult) -> str | None:
        """LOG half (SPEC §5 step 5): validate and append one §7 episode record.

        The record is assembled from ``result`` plus the stored fingerprint
        (``tools_used`` lists the episode's toolchain: the discovery callable
        always, plus the downloader when it is configured and the run is not a
        dry run - the only mode in which ``run()`` ever invokes it). It is
        passed through :func:`src.agents.episodes.normalize_episode` BEFORE
        anything is written, so a malformed episode fails loudly here, never
        silently in prod, then appended to the journal when one is configured
        - a dry run logs too. A run that worked at least one §11.3 escalation
        ticket is stamped ``trigger="escalation"`` +
        ``channel=<last channel worked>`` (:attr:`_escalation_channel`, set by
        :meth:`_work_escalations`); every other run keeps both keys ``None``.
        Returns the ``episode_id`` for ``AgentResult.episode_id``; without a
        journal nothing is written and ``None`` is returned. The validated
        record stays on :attr:`episode` for the UPDATE half.
        """
        now = self.now
        episode_id = episodes.new_episode_id(self.mf_id, result.strategy, now=now)
        tools_used = ["discover"]
        if not result.dry_run and self.download is not None:
            tools_used.append("download")
        episode = {
            "timestamp": now,
            "episode_id": episode_id,
            "mf_id": self.mf_id,
            "amc_name": self.amc_name,
            "fingerprint": dict(self.fingerprint),
            "strategy_applied": result.strategy,
            "tools_used": tools_used,
            "outcome": result.outcome,
            "failure_code": result.failure_code,
            "reward": result.reward,
            "discovered_count": result.discovered_count,
            "downloaded_count": result.downloaded_count,
            "evidence": {"sample_url": None, "elapsed_sec": None},
            "trigger": TRIGGER_ESCALATION if self._escalation_channel is not None else None,
            "channel": self._escalation_channel,
        }
        record = episodes.normalize_episode(episode, now=now)
        self.episode = record
        if self.journal is None:
            return None
        self.journal.append(record, now=now)
        return episode_id

    def _report(self, result: AgentResult) -> AgentResult:
        """UPDATE/REPORT half (SPEC §5 steps 6-7).

        Folds the validated episode (:attr:`episode`, built by the LOG half)
        into the playbook register with
        :func:`src.agents.bandit.update_from_episode` - the register is
        ``knowledge.load(self.register_path)`` when a path is configured, an
        empty dict otherwise - and persists it with
        :func:`src.agents.knowledge.save` when ``register_path`` is set.
        Returns ``result`` unchanged.
        """
        if self.episode is None:
            return result
        register = knowledge.load(self.register_path) if self.register_path is not None else {}
        bandit.update_from_episode(register, self.episode, now=self.now)
        if self.register_path is not None:
            knowledge.save(register, self.register_path, now=self.now)
        return result


__all__ = [
    "Agent",
    "AgentResult",
    "ChannelRunnerFn",
    "DownloadFn",
    "OUTCOME_FAILED",
    "OUTCOME_PARTIAL",
    "OUTCOME_SUCCESS",
    "OUTCOMES",
    "compute_reward",
]
