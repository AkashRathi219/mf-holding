"""[T5] Per-AMC agent loop (SPEC §5): OBSERVE -> PLAN -> ACT -> OBSERVE RESULT
(steps 1-4) plus the LOG -> UPDATE -> REPORT half (steps 5-7). The breaker
short-circuits before any discover call, a dry run never touches the
downloader, SUCCESS always clears the failure code, and every non-SUCCESS
outcome carries a valid taxonomy code. Every run - dry runs included - appends
one schema-valid episode to the injected journal, FAILED episodes carry the
result's taxonomy code, history is append-only, and the UPDATE half folds the
episode into the register and persists it. [T21] escalation consumption
(SPEC §11.3): pending OPEN tickets for this AMC are dispatched to
``next_channel`` through the injected ``channel_runner`` BEFORE the normal
loop, attempts are recorded on a real tmp-path ``EscalationQueue`` (success
closes, the queue's own N_max cap parks), the agent never hands the channel
the strategy that produced the incomplete parse, escalation-driven runs stamp
``trigger="escalation"``/``channel=<name>`` on the written episode, a dry run
dispatches no channel and never records an attempt (the persisted queue stays
untouched) while a non-dry run still does both, and no queue/runner
configuration changes nothing. All collaborators are fakes; zero
network, tmp_path only."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from src.agents import knowledge
from src.agents.agent import Agent, AgentResult, compute_reward
from src.agents.bandit import fingerprint_hash, seeded_rng
from src.agents.channels import ChannelResult
from src.agents.episodes import EPISODE_SCHEMA_KEYS, EpisodeJournal, month_of, normalize_episode
from src.agents.escalation import (
    CHANNELS,
    N_MAX,
    STATUS_CLOSED,
    STATUS_MANUAL,
    STATUS_OPEN,
    EscalationQueue,
    Ticket,
)
from src.agents.taxonomy import FAILURE_CODES, is_valid_code

T0 = "2026-10-04T06:15:00+05:30"
T1 = "2026-10-04T06:15:01+05:30"
LINKS = [
    "https://amc.example/monthly.pdf",
    "https://amc.example/factsheet.pdf",
    "https://amc.example/workbook.xlsx",
]


class FakeDownload:
    def __init__(self, result: object = 0) -> None:
        self.calls: list[list] = []
        self.result = result

    def __call__(self, links: list) -> object:
        self.calls.append(list(links))
        return self.result


class BlockedState:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def is_blocked(self, host: str) -> bool:
        self.checked.append(host)
        return True


class OpenState:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def is_blocked(self, host: str) -> bool:
        self.checked.append(host)
        return False


class FakeRunner:
    """Scripted ``channel_runner``: records every dispatch, pops results in order."""

    def __init__(self, *results: ChannelResult) -> None:
        self.calls: list[dict] = []
        self._results = list(results)

    def __call__(
        self, channel: str, ticket: Ticket, *, failed_strategy: str | None = None
    ) -> ChannelResult:
        self.calls.append(
            {"channel": channel, "ticket": ticket, "failed_strategy": failed_strategy}
        )
        if self._results:
            return self._results.pop(0)
        return ChannelResult(
            channel=channel,
            success=False,
            reason="no scripted result; reported as a failed attempt",
            failure_code="ERR_SCHEME_MISSING_IN_DB",
        )


def _queue(tmp_path: Path) -> EscalationQueue:
    return EscalationQueue(
        queue_path=tmp_path / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "manual_intervention.csv",
    )


def _enqueue_t2(
    q: EscalationQueue,
    amc: str = "Test AMC",
    scheme: str = "Test Fund",
    month: str = "2026-08",
) -> Ticket:
    ticket = q.enqueue(amc, scheme, month, "T2", 60.0, "full_portfolio")
    assert ticket is not None
    return ticket


def test_compute_reward_matches_documented_formula():
    assert compute_reward(3, 2, True) == 7.0
    assert compute_reward(0, 5, True) == 10.0
    assert compute_reward(4, 0, True) == 4.0
    assert compute_reward(0, 0, True) == 0.0
    assert compute_reward(3, 2, False) == 0.0
    assert compute_reward(9, 9, False) == 0.0


def test_successful_dry_run_never_calls_download():
    download = FakeDownload(result=2)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        download=download,
        dry_run=True,
    )

    result = agent.run()

    assert isinstance(result, AgentResult)
    assert result.outcome == "SUCCESS"
    assert result.blocked is False
    assert result.dry_run is True
    assert result.discovered_count == len(LINKS)
    assert result.downloaded_count == 0
    assert result.failure_code is None
    assert result.reward == 1.0 * len(LINKS)
    assert download.calls == []


def test_ladder_failure_yields_failed_with_valid_taxonomy_code():
    download = FakeDownload(result=3)

    def discover(strategy: str, amc_name: str) -> list:
        return []

    agent = Agent("53", "Test AMC", discover=discover, download=download)

    result = agent.run()

    assert result.outcome == "FAILED"
    assert result.failure_code is not None
    assert result.failure_code in FAILURE_CODES
    assert is_valid_code(result.failure_code)
    assert result.blocked is False
    assert result.discovered_count == 0
    assert result.downloaded_count == 0
    assert result.reward == 0.0
    assert download.calls == []


def test_blocked_host_short_circuits_before_any_discover_call():
    state = BlockedState()
    calls: list[str] = []

    def discover(strategy: str, amc_name: str) -> list:
        calls.append(strategy)
        return list(LINKS)

    agent = Agent("53", "Test AMC", discover=discover, state=state)

    result = agent.run()

    assert state.checked == ["Test AMC"]
    assert calls == []
    assert result.blocked is True
    assert result.outcome == "FAILED"
    assert result.failure_code is not None
    assert is_valid_code(result.failure_code)
    assert result.discovered_count == 0
    assert result.downloaded_count == 0
    assert result.reward == 0.0


def test_success_always_implies_none_failure_code():
    cases = [
        ("dry run", {"dry_run": True}),
        ("count download", {"download": FakeDownload(result=2)}),
        ("list download", {"download": FakeDownload(result=["saved/a.pdf"])}),
    ]
    for label, kwargs in cases:
        agent = Agent(
            "53",
            "Test AMC",
            discover=lambda strategy, amc_name: list(LINKS),
            **kwargs,
        )
        result = agent.run()
        assert result.outcome == "SUCCESS", label
        assert result.failure_code is None, label
        assert result.reward == 1.0 * len(LINKS) + 2.0 * result.downloaded_count, label


def test_planned_strategy_lands_on_the_result(tmp_path):
    agent = Agent("53", "Test AMC", discover=lambda strategy, amc_name: [])
    assert agent.run().strategy == "fast_http"

    register = knowledge.empty_register(now=T0)
    register["playbooks"]["53"] = {
        "amc_name": "Test AMC",
        "fingerprint_hash": fingerprint_hash({"cdn": "", "cms": "", "auth": ""}),
        "best_strategy": "curl_impersonate",
        "confidence_score": 9.9,
        "parameters": {},
        "known_quirks": [],
        "observations": 4,
        "last_updated": T0,
        "decay_applied_at": None,
    }
    register_path = tmp_path / "amc_playbooks.json"
    knowledge.save(register, register_path, now=T0)

    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: [],
        register_path=register_path,
        rng=seeded_rng(7),
    )
    result = agent.run()

    assert result.strategy == "curl_impersonate"


# ---------------------------------------------------------------------------
# LOG/UPDATE/REPORT half (SPEC §5 steps 5-7)
# ---------------------------------------------------------------------------

def _journal(tmp_path: Path) -> EpisodeJournal:
    return EpisodeJournal(root=tmp_path / "agent_episodes")


def _month_file(tmp_path: Path) -> Path:
    return _journal(tmp_path).path_for("53", "Test AMC", month_of(T0))


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_normal_run_appends_one_episode_and_sets_result_episode_id(tmp_path):
    journal = _journal(tmp_path)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        download=FakeDownload(result=2),
        journal=journal,
        now=T0,
    )

    result = agent.run()

    assert result.episode_id is not None
    path = _month_file(tmp_path)
    assert path.exists()
    lines = _lines(path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["episode_id"] == result.episode_id
    assert record["mf_id"] == "53"
    assert record["amc_name"] == "Test AMC"
    assert record["outcome"] == "SUCCESS"
    assert record["failure_code"] is None
    assert record["discovered_count"] == len(LINKS)
    assert record["downloaded_count"] == 2
    assert record["reward"] == 7.0


def test_dry_run_still_writes_an_episode(tmp_path):
    journal = _journal(tmp_path)
    download = FakeDownload(result=5)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        download=download,
        journal=journal,
        dry_run=True,
        now=T0,
    )

    result = agent.run()

    assert download.calls == []
    assert result.episode_id is not None
    assert result.outcome == "SUCCESS"
    lines = _lines(_month_file(tmp_path))
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outcome"] == "SUCCESS"
    assert record["downloaded_count"] == 0
    assert record["tools_used"] == ["discover"]


def test_written_episode_is_schema_valid_on_reload(tmp_path):
    journal = _journal(tmp_path)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        download=FakeDownload(result=2),
        journal=journal,
        now=T0,
    )
    result = agent.run()

    record = journal.load(_month_file(tmp_path))[result.episode_id]
    normalized = normalize_episode(record)

    assert set(normalized) == set(EPISODE_SCHEMA_KEYS)
    assert normalized == record
    assert normalized["episode_id"] == result.episode_id


def test_failed_run_writes_episode_with_matching_taxonomy_code(tmp_path):
    journal = _journal(tmp_path)

    def discover(strategy: str, amc_name: str) -> list:
        return []

    agent = Agent("53", "Test AMC", discover=discover, journal=journal, now=T0)
    result = agent.run()

    assert result.outcome == "FAILED"
    assert result.failure_code is not None
    record = journal.load(_month_file(tmp_path))[result.episode_id]
    assert record["outcome"] == "FAILED"
    assert record["failure_code"] == result.failure_code
    assert record["failure_code"] in FAILURE_CODES
    assert is_valid_code(record["failure_code"])


def test_two_sequential_runs_append_two_lines(tmp_path):
    journal = _journal(tmp_path)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        journal=journal,
        now=T0,
    )

    first = agent.run()
    agent.now = T1
    second = agent.run()

    path = _month_file(tmp_path)
    lines = _lines(path)
    assert len(lines) == 2
    assert first.episode_id != second.episode_id
    assert journal.count(path) == 2
    assert json.loads(lines[0])["episode_id"] == first.episode_id
    assert json.loads(lines[1])["episode_id"] == second.episode_id


def test_update_persists_register_with_the_amc_key(tmp_path):
    journal = _journal(tmp_path)
    register_path = tmp_path / "amc_playbooks.json"
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        journal=journal,
        register_path=register_path,
        dry_run=True,
        now=T0,
    )

    result = agent.run()

    assert register_path.exists()
    register = knowledge.load(register_path, now=T0)
    assert "53" in register["playbooks"]
    playbook = register["playbooks"]["53"]
    assert playbook["amc_name"] == "Test AMC"
    assert playbook["best_strategy"] == result.strategy
    assert playbook["observations"] == 1
    assert playbook["confidence_score"] == 0.6


# ---------------------------------------------------------------------------
# Escalation consumption (SPEC §11.3, T21): queue + channel dispatch
# ---------------------------------------------------------------------------

def test_pending_ticket_is_dispatched_to_the_next_channel_in_order(tmp_path):
    q = _queue(tmp_path)
    fresh = _enqueue_t2(q, scheme="Fund A")
    advanced = _enqueue_t2(q, scheme="Fund B")
    q.record_attempt(advanced, "amc_recheck", False)
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: [],
        queue=q,
        channel_runner=runner,
        now=T0,
    )

    result = agent.run()

    assert result.escalations_worked == 2
    assert result.escalations_closed == 0
    assert [call["channel"] for call in runner.calls] == ["amc_recheck", "web_search"]
    assert runner.calls[0]["ticket"].queue_id == fresh.queue_id
    assert runner.calls[1]["ticket"].queue_id == advanced.queue_id


def test_forced_alternate_failed_strategy_is_not_the_incomplete_parse_strategy(tmp_path):
    q = _queue(tmp_path)
    first = Agent("53", "Test AMC", discover=lambda strategy, amc_name: list(LINKS), now=T0)
    first.run()
    assert first.episode is not None
    assert first.episode["strategy_applied"] == "fast_http"
    _enqueue_t2(q)

    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        queue=q,
        channel_runner=runner,
        now=T1,
    )
    agent.run()

    assert runner.calls
    # AC-18 forced-alternate: the agent must not hand the channel the strategy
    # that produced the incomplete parse (its own ladder strategy); it keeps no
    # failed PARSE strategy at all, so the channel owns the alternate rotation.
    assert runner.calls[0]["failed_strategy"] != "fast_http"
    assert runner.calls[0]["failed_strategy"] is None


def test_successful_channel_result_closes_the_ticket_and_counts_it(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    runner = FakeRunner(
        ChannelResult(
            channel="amc_recheck",
            success=True,
            reason="closed by full_portfolio document (parse:regex_holdings)",
            strategy_used="regex_holdings",
        )
    )
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: [],
        queue=q,
        channel_runner=runner,
        now=T0,
    )

    result = agent.run()

    assert result.escalations_worked == 1
    assert result.escalations_closed == 1
    # the agent works the queue FOLD's ticket instance, so the written state -
    # exactly what record_attempt(..., True) produces - is the evidence
    folded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.queue_id == ticket.queue_id
    assert folded.status == STATUS_CLOSED
    assert folded.attempts == 1
    assert folded.channels_tried == ["amc_recheck"]
    assert not (tmp_path / "manual_intervention.csv").exists()


def test_failed_channel_result_records_the_attempt_and_queue_parks_at_cap(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: [],
        queue=q,
        channel_runner=runner,
        now=T0,
    )

    result = agent.run()

    fetch_channels = [c for c in CHANNELS if c != "manual"]
    assert result.escalations_worked == 1
    assert result.escalations_closed == 0
    folded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.status == STATUS_OPEN
    assert folded.attempts == 1
    assert folded.channels_tried == fetch_channels[:1]
    assert not (tmp_path / "manual_intervention.csv").exists()

    for i in range(1, N_MAX):
        agent.now = f"2026-10-04T06:15:{i:02d}+05:30"
        agent.run()

    assert [call["channel"] for call in runner.calls] == fetch_channels[:N_MAX]
    folded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.status == STATUS_MANUAL
    assert folded.attempts == N_MAX
    with open(tmp_path / "manual_intervention.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["queue_id"] == ticket.queue_id


def test_escalation_run_logs_episode_with_trigger_and_channel(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    journal = _journal(tmp_path)
    runner = FakeRunner(
        ChannelResult(
            channel="amc_recheck",
            success=True,
            reason="closed by full_portfolio document",
            strategy_used="regex_holdings",
        )
    )
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        journal=journal,
        queue=q,
        channel_runner=runner,
        now=T0,
    )

    result = agent.run()

    record = journal.load(_month_file(tmp_path))[result.episode_id]
    assert record["trigger"] == "escalation"
    assert record["channel"] == "amc_recheck"


def test_dry_run_performs_no_download_and_still_logs(tmp_path):
    journal = _journal(tmp_path)
    download = FakeDownload(result=5)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        download=download,
        journal=journal,
        dry_run=True,
        now=T0,
    )

    result = agent.run()

    assert download.calls == []
    assert result.episode_id is not None
    assert result.outcome == "SUCCESS"
    lines = _lines(_month_file(tmp_path))
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outcome"] == "SUCCESS"
    assert record["downloaded_count"] == 0
    assert record["trigger"] is None
    assert record["channel"] is None


def test_no_queue_or_runner_configured_changes_nothing(tmp_path):
    journal = _journal(tmp_path)
    download = FakeDownload(result=2)
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        download=download,
        journal=journal,
        now=T0,
    )

    result = agent.run()

    assert result.escalations_worked == 0
    assert result.escalations_closed == 0
    assert result.outcome == "SUCCESS"
    record = journal.load(_month_file(tmp_path))[result.episode_id]
    assert record["trigger"] is None
    assert record["channel"] is None
    assert agent.pending_tickets("Test AMC") == []

    only_queue = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        queue=_queue(tmp_path),
        now=T1,
    )
    only_runner = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        channel_runner=FakeRunner(),
        now=T1,
    )
    assert only_queue.run().escalations_worked == 0
    assert only_runner.run().escalations_worked == 0


def test_tickets_for_a_different_amc_are_not_touched(tmp_path):
    q = _queue(tmp_path)
    other = _enqueue_t2(q, amc="Other AMC")
    parked = _enqueue_t2(q)
    q.park(parked, "SCHEME_DISCONTINUED")
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: [],
        queue=q,
        channel_runner=runner,
        now=T0,
    )

    result = agent.run()

    assert runner.calls == []
    assert result.escalations_worked == 0
    assert result.escalations_closed == 0
    assert agent.pending_tickets("Test AMC") == []
    assert [t.queue_id for t in agent.pending_tickets("Other AMC")] == [other.queue_id]
    folded = q.load()[("Other AMC", "Test Fund", "2026-08")]
    assert folded.status == STATUS_OPEN
    assert folded.attempts == 0
    assert folded.channels_tried == []


def test_blocked_host_skips_the_escalation_phase_entirely(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    state = BlockedState()
    runner = FakeRunner()
    calls: list[str] = []

    def discover(strategy: str, amc_name: str) -> list:
        calls.append(strategy)
        return list(LINKS)

    agent = Agent(
        "53",
        "Test AMC",
        discover=discover,
        queue=q,
        channel_runner=runner,
        state=state,
        now=T0,
    )

    result = agent.run()

    assert runner.calls == []
    assert state.checked == ["Test AMC"]
    assert calls == []
    assert result.blocked is True
    assert result.outcome == "FAILED"
    assert result.failure_code == "ERR_WAF_CLOUDFLARE_1015"
    assert result.escalations_worked == 0
    assert result.escalations_closed == 0
    folded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.status == STATUS_OPEN
    assert folded.attempts == 0
    assert folded.channels_tried == []


def test_unblocked_host_still_dispatches_pending_tickets(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    state = OpenState()
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: [],
        queue=q,
        channel_runner=runner,
        state=state,
        now=T0,
    )

    result = agent.run()

    assert state.checked == ["Test AMC"]
    assert [call["channel"] for call in runner.calls] == ["amc_recheck"]
    assert result.escalations_worked == 1
    assert result.escalations_closed == 0
    assert result.blocked is False


def test_dry_run_with_pending_tickets_still_writes_an_episode(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    journal = _journal(tmp_path)
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        journal=journal,
        queue=q,
        channel_runner=runner,
        dry_run=True,
        now=T0,
    )

    result = agent.run()

    assert result.episode_id is not None
    assert result.escalations_worked == 0
    lines = _lines(_month_file(tmp_path))
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outcome"] == "SUCCESS"
    assert record["trigger"] is None
    assert record["channel"] is None


def test_dry_run_never_dispatches_escalations_or_records_attempts(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        queue=q,
        channel_runner=runner,
        dry_run=True,
        now=T0,
    )

    result = agent.run()

    assert runner.calls == []
    assert result.escalations_worked == 0
    assert result.escalations_closed == 0
    # re-read the queue from disk through a fresh instance: the persisted
    # ticket must be exactly as enqueued - a rehearsal never mutates it
    persisted = _queue(tmp_path).load()[(ticket.amc, ticket.scheme, ticket.month)]
    assert persisted.queue_id == ticket.queue_id
    assert persisted.status == STATUS_OPEN
    assert persisted.attempts == 0
    assert persisted.channels_tried == []
    assert not (tmp_path / "manual_intervention.csv").exists()


def test_live_run_still_dispatches_and_records_attempts(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    runner = FakeRunner()
    agent = Agent(
        "53",
        "Test AMC",
        discover=lambda strategy, amc_name: list(LINKS),
        queue=q,
        channel_runner=runner,
        dry_run=False,
        now=T0,
    )

    result = agent.run()

    assert [call["channel"] for call in runner.calls] == ["amc_recheck"]
    assert result.escalations_worked == 1
    assert result.escalations_closed == 0
    folded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.status == STATUS_OPEN
    assert folded.attempts == 1
    assert folded.channels_tried == ["amc_recheck"]
