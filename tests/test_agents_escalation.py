"""[T19] Escalation queue tests (SPEC §11.3/§11.4 / AC-15, AC-16).

Covers the append-only JSONL queue with ``(amc, scheme, month)`` dedupe and
last-write-wins folding, the T2-only enqueue gate (a T1 top-10 fallback must
never enter the queue), the §11.3 channel order via ``next_channel``, the
``N_max`` attempt cap (one per fetch channel) parking a ticket as MANUAL
with a manual-register
row, a successful attempt closing the ticket, MANUAL immutability (no
automated call may change a parked ticket or duplicate its register row),
defensive skipping of a corrupt/partial trailing line, and tmp-path
isolation: every test injects explicit paths and the real
``data/logs/escalation_queue.jsonl`` / ``data/reference/manual_intervention.csv``
must never be created.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.agents.escalation import (
    CHANNELS,
    DEFAULT_MANUAL_CSV_PATH,
    DEFAULT_QUEUE_PATH,
    MANUAL_COLUMNS,
    N_MAX,
    REASON_ALL_CHANNELS_EXHAUSTED,
    REASON_CODES,
    STATUS_CLOSED,
    STATUS_MANUAL,
    STATUS_OPEN,
    EscalationQueue,
    Ticket,
    next_channel,
)

QUEUE_JSONL = "escalation_queue.jsonl"
MANUAL_CSV = "manual_intervention.csv"

QUEUE_SCHEMA_KEYS = {
    "queue_id",
    "amc",
    "scheme",
    "month",
    "tier",
    "coverage_pct",
    "document_class",
    "channels_tried",
    "attempts",
    "first_seen",
    "last_tried",
    "status",
}


def _queue(tmp_path: Path) -> EscalationQueue:
    return EscalationQueue(
        queue_path=tmp_path / QUEUE_JSONL,
        manual_csv_path=tmp_path / MANUAL_CSV,
    )


def _enqueue_t2(
    q: EscalationQueue,
    amc: str = "Test AMC",
    scheme: str = "Test Fund",
    month: str = "2026-08",
    coverage: float = 60.0,
):
    return q.enqueue(amc, scheme, month, "T2", coverage, "full_portfolio")


def _ticket(channels: list[str]) -> Ticket:
    return Ticket(
        queue_id="ESC-test",
        amc="A",
        scheme="S",
        month="2026-08",
        tier="T2",
        coverage_pct=60.0,
        document_class="full_portfolio",
        channels_tried=list(channels),
        attempts=len(channels),
        first_seen="2026-10-04T00:00:00+00:00",
        last_tried="",
        status=STATUS_OPEN,
    )


def _register_rows(tmp_path: Path) -> list[dict[str, str]]:
    with open(tmp_path / MANUAL_CSV, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _queue_lines(tmp_path: Path) -> list[str]:
    return (tmp_path / QUEUE_JSONL).read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# constants: channel order, N_max, default paths, register columns, reasons
# ---------------------------------------------------------------------------

def test_constants_match_spec():
    assert CHANNELS == ("amc_recheck", "web_search", "amfi", "advisorkhoj", "manual")
    # The cap must cover every fetch channel so none is silently skipped.
    assert N_MAX == len([c for c in CHANNELS if c != "manual"])
    assert DEFAULT_QUEUE_PATH == Path("data/logs/escalation_queue.jsonl")
    assert DEFAULT_MANUAL_CSV_PATH == Path("data/reference/manual_intervention.csv")
    assert set(REASON_CODES) == {
        "NOT_PUBLISHED_BY_AMC",
        "ALL_CHANNELS_EXHAUSTED",
        "PARSE_IMPOSSIBLE",
        "SCHEME_DISCONTINUED",
        "DUPLICATE_PLAN",
    }
    assert MANUAL_COLUMNS == (
        "queue_id",
        "amc",
        "scheme",
        "month",
        "coverage_pct",
        "tier",
        "document_class",
        "channels_tried",
        "attempts",
        "reason_code",
        "first_seen",
        "last_tried",
        "assigned_to",
        "status",
        "notes",
    )


# ---------------------------------------------------------------------------
# 1. AC-15 dedupe: appending the same (amc, scheme, month) twice -> 1 OPEN
# ---------------------------------------------------------------------------

def test_enqueue_same_key_twice_yields_one_open_ticket(tmp_path):
    q = _queue(tmp_path)
    first = _enqueue_t2(q)
    second = _enqueue_t2(q)
    assert first is not None and second is not None
    assert second.queue_id == first.queue_id
    tickets = q.load()
    assert list(tickets) == [("Test AMC", "Test Fund", "2026-08")]
    ticket = tickets[("Test AMC", "Test Fund", "2026-08")]
    assert ticket.status == STATUS_OPEN
    assert ticket.attempts == 0
    assert ticket.channels_tried == []
    assert len(_queue_lines(tmp_path)) == 1


def test_enqueue_rejects_t1_and_other_tiers(tmp_path):
    q = _queue(tmp_path)
    assert q.enqueue("A", "S", "2026-08", "T1", 45.0, "factsheet_topn") is None
    assert q.enqueue("A", "S", "2026-08", "T0", 97.0, "full_portfolio") is None
    assert q.enqueue("A", "S", "2026-08", "T3", 0.0, "full_portfolio") is None
    assert not (tmp_path / QUEUE_JSONL).exists()


def test_enqueue_blocked_while_manual(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    q.park(ticket, "SCHEME_DISCONTINUED")
    again = _enqueue_t2(q)
    assert again is not None
    assert again.queue_id == ticket.queue_id
    assert again.status == STATUS_MANUAL
    assert again.attempts == 0
    assert len(_queue_lines(tmp_path)) == 2


def test_enqueue_after_closed_opens_fresh_ticket(tmp_path):
    q = _queue(tmp_path)
    first = _enqueue_t2(q)
    q.record_attempt(first, "amc_recheck", True)
    second = _enqueue_t2(q)
    assert second is not None
    assert second.queue_id != first.queue_id
    assert second.status == STATUS_OPEN
    tickets = q.load()
    assert len(tickets) == 1
    assert tickets[("Test AMC", "Test Fund", "2026-08")].queue_id == second.queue_id


def test_queue_line_schema_matches_spec(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    record = json.loads(_queue_lines(tmp_path)[0])
    assert set(record) == QUEUE_SCHEMA_KEYS
    assert record["status"] == "OPEN"
    assert record["channels_tried"] == []
    assert record["tier"] == "T2"
    assert record["coverage_pct"] == 60.0
    assert record["document_class"] == "full_portfolio"
    assert record["queue_id"] == ticket.queue_id
    assert record["first_seen"]


# ---------------------------------------------------------------------------
# 2/3. §11.3 channel order via next_channel
# ---------------------------------------------------------------------------

def test_next_channel_walks_channels_in_order():
    ticket = _ticket([])
    assert next_channel(ticket) == "amc_recheck"
    ticket.channels_tried.append("amc_recheck")
    assert next_channel(ticket) == "web_search"
    ticket.channels_tried.append("web_search")
    assert next_channel(ticket) == "amfi"
    ticket.channels_tried.append("amfi")
    assert next_channel(ticket) == "advisorkhoj"
    ticket.channels_tried.append("advisorkhoj")
    assert next_channel(ticket) == "manual"
    ticket.channels_tried.append("manual")
    assert next_channel(ticket) == "manual"


def test_live_channel_walk_follows_order_until_cap(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    for channel in fetch_channels[:N_MAX]:
        assert next_channel(ticket) == channel
        assert ticket.status == STATUS_OPEN
        q.record_attempt(ticket, channel, False)
    assert next_channel(ticket) == "manual"
    assert ticket.status == STATUS_MANUAL


def test_record_attempt_rejects_unknown_channel(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    with pytest.raises(ValueError):
        q.record_attempt(ticket, "carrier_pigeon", False)
    assert ticket.attempts == 0
    assert ticket.channels_tried == []


def test_record_attempt_does_not_duplicate_channel(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    q.record_attempt(ticket, "amc_recheck", False)
    q.record_attempt(ticket, "amc_recheck", False)
    assert ticket.channels_tried == ["amc_recheck"]
    assert ticket.attempts == 2
    assert ticket.status == STATUS_OPEN


# ---------------------------------------------------------------------------
# 4. N_max failed attempts -> MANUAL + manual-register row with reason_code
#    The cap MUST equal the number of fetch channels, otherwise a later
#    channel (advisorkhoj) is unreachable and the ladder silently skips it.
# ---------------------------------------------------------------------------

def test_attempt_cap_covers_every_fetch_channel():
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    assert N_MAX >= len(fetch_channels), (
        f"N_MAX={N_MAX} is below the {len(fetch_channels)} fetch channels "
        f"{fetch_channels}; the last channel(s) would never run")


def test_every_fetch_channel_is_reachable_before_parking(tmp_path):
    """Walk the ladder with every channel failing; assert each fetch channel
    got its turn and only then did the ticket park."""
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    tried = []
    for _ in range(N_MAX):
        channel = next_channel(ticket)
        tried.append(channel)
        q.record_attempt(ticket, channel, False)
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    assert tried == fetch_channels
    assert ticket.status == STATUS_MANUAL


def test_three_failed_attempts_park_manual_with_register_row(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    for channel in ("amc_recheck", "web_search", "amfi", "advisorkhoj")[:N_MAX]:
        q.record_attempt(ticket, channel, False)
    assert ticket.status == STATUS_MANUAL
    assert ticket.attempts == N_MAX
    rows = _register_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["queue_id"] == ticket.queue_id
    assert row["reason_code"] == REASON_ALL_CHANNELS_EXHAUSTED
    assert row["reason_code"] in REASON_CODES
    assert row["amc"] == "Test AMC"
    assert row["scheme"] == "Test Fund"
    assert row["month"] == "2026-08"
    assert row["status"] == STATUS_MANUAL
    assert row["assigned_to"] == ""
    assert row["notes"] == ""


def test_register_header_and_parked_tier(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    q.park(ticket, "NOT_PUBLISHED_BY_AMC")
    header = (tmp_path / MANUAL_CSV).read_text(encoding="utf-8").splitlines()[0]
    assert header == (
        "queue_id,amc,scheme,month,coverage_pct,tier,document_class,channels_tried,"
        "attempts,reason_code,first_seen,last_tried,assigned_to,status,notes"
    )
    rows = _register_rows(tmp_path)
    assert rows[0]["tier"] == "T3"
    assert rows[0]["reason_code"] == "NOT_PUBLISHED_BY_AMC"
    assert rows[0]["channels_tried"] == ""


def test_park_forces_manual_and_persists(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    parked = q.park(ticket, "PARSE_IMPOSSIBLE")
    assert parked is ticket
    assert ticket.status == STATUS_MANUAL
    reloaded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert reloaded.status == STATUS_MANUAL
    assert reloaded.queue_id == ticket.queue_id


def test_park_rejects_unknown_reason_code(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    with pytest.raises(ValueError):
        q.park(ticket, "SOMETHING_ELSE")
    assert ticket.status == STATUS_OPEN
    assert not (tmp_path / MANUAL_CSV).exists()


# ---------------------------------------------------------------------------
# 5. success -> CLOSED
# ---------------------------------------------------------------------------

def test_successful_attempt_closes_ticket(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    q.record_attempt(ticket, "amc_recheck", True)
    assert ticket.status == STATUS_CLOSED
    assert ticket.attempts == 1
    assert ticket.channels_tried == ["amc_recheck"]
    assert ticket.last_tried
    assert not (tmp_path / MANUAL_CSV).exists()
    reloaded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert reloaded.status == STATUS_CLOSED
    assert reloaded.attempts == 1


def test_state_folds_across_instances_last_write_wins(tmp_path):
    q1 = _queue(tmp_path)
    ticket = _enqueue_t2(q1)
    q1.record_attempt(ticket, "amc_recheck", False)
    q2 = _queue(tmp_path)
    folded = q2.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.attempts == 1
    assert folded.channels_tried == ["amc_recheck"]
    assert folded.status == STATUS_OPEN
    assert folded.first_seen == ticket.first_seen


def test_load_missing_file_returns_empty_dict(tmp_path):
    assert _queue(tmp_path).load() == {}


# ---------------------------------------------------------------------------
# 6. corrupt / partial trailing line is skipped by load() without raising
# ---------------------------------------------------------------------------

def test_load_skips_partial_trailing_line(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    with open(tmp_path / QUEUE_JSONL, "a", encoding="utf-8") as fh:
        fh.write('{"queue_id": "ESC-broken", "amc": "A", "sch')
    tickets = q.load()
    assert list(tickets) == [("Test AMC", "Test Fund", "2026-08")]
    assert tickets[("Test AMC", "Test Fund", "2026-08")].status == STATUS_OPEN


def test_load_skips_unparseable_full_line(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    with open(tmp_path / QUEUE_JSONL, "a", encoding="utf-8") as fh:
        fh.write("not-json-at-all\n")
    assert len(q.load()) == 1


def test_load_skips_record_missing_key_fields(tmp_path):
    q = _queue(tmp_path)
    _enqueue_t2(q)
    with open(tmp_path / QUEUE_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"queue_id": "ESC-x", "status": "OPEN"}) + "\n")
    assert len(q.load()) == 1


def test_append_after_partial_tail_does_not_glue(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    with open(tmp_path / QUEUE_JSONL, "a", encoding="utf-8") as fh:
        fh.write('{"partial"')
    q.record_attempt(ticket, "amc_recheck", False)
    folded = q.load()[("Test AMC", "Test Fund", "2026-08")]
    assert folded.attempts == 1
    assert folded.channels_tried == ["amc_recheck"]


# ---------------------------------------------------------------------------
# MANUAL immutability: no automated call may touch a parked ticket (§11.4)
# ---------------------------------------------------------------------------

def test_manual_ticket_is_immutable_to_automated_calls(tmp_path):
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    for channel in [c for c in CHANNELS if c != "manual"]:
        q.record_attempt(ticket, channel, False)
    assert ticket.status == STATUS_MANUAL
    queue_before = (tmp_path / QUEUE_JSONL).read_text(encoding="utf-8")
    register_before = (tmp_path / MANUAL_CSV).read_text(encoding="utf-8")

    q.record_attempt(ticket, "advisorkhoj", True)
    assert ticket.status == STATUS_MANUAL
    assert ticket.attempts == N_MAX
    assert ticket.channels_tried == ["amc_recheck", "web_search", "amfi", "advisorkhoj"]
    assert (tmp_path / QUEUE_JSONL).read_text(encoding="utf-8") == queue_before
    assert (tmp_path / MANUAL_CSV).read_text(encoding="utf-8") == register_before

    q.park(ticket, "PARSE_IMPOSSIBLE")
    rows = _register_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == REASON_ALL_CHANNELS_EXHAUSTED
    assert (tmp_path / QUEUE_JSONL).read_text(encoding="utf-8") == queue_before


# ---------------------------------------------------------------------------
# 7. tmp-path isolation: the real data files are never created
# ---------------------------------------------------------------------------

def test_real_data_files_are_never_created(tmp_path):
    # The real data files legitimately exist once the integrity auditor has run
    # in production (AC-15), so assert they are UNCHANGED by these tests rather
    # than asserting they are absent -- that preserves the isolation intent
    # without failing once real data lands.
    def _fingerprint(path):
        p = Path(path)
        if not p.exists():
            return None
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)

    before = (_fingerprint(DEFAULT_QUEUE_PATH),
              _fingerprint(DEFAULT_MANUAL_CSV_PATH))
    q = _queue(tmp_path)
    ticket = _enqueue_t2(q)
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    for channel in fetch_channels[:N_MAX]:
        q.record_attempt(ticket, channel, False)
    assert ticket.status == STATUS_MANUAL
    assert (tmp_path / QUEUE_JSONL).exists()
    assert (tmp_path / MANUAL_CSV).exists()
    assert (_fingerprint(DEFAULT_QUEUE_PATH),
            _fingerprint(DEFAULT_MANUAL_CSV_PATH)) == before
