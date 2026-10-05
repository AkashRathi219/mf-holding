"""[T35] Manual intervention register tests (SPEC §11.4 / OQ-11 / AC-20).

Covers the tolerant reader over ``data/reference/manual_intervention.csv`` -
a missing file reads as no rows without raising, malformed rows are skipped,
and a CSV written by the real ``escalation.park()``/``record_attempt`` writer
round-trips with the documented §11.4 columns - plus the pending/open and
amc/queue_id filters, the reason-code summary, and the human-authoritative
invariant: the ONLY writer of a resolved status is ``mark_resolved`` (the
human entry point), which refuses unknown queue_ids and records who/when;
every other public function is read-only or append-only annotation and
provably leaves the status column untouched.  Tmp-path isolation throughout:
the real register file is never created or modified.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.agents import manual_register
from src.agents.escalation import (
    CHANNELS,
    DEFAULT_MANUAL_CSV_PATH,
    MANUAL_COLUMNS,
    N_MAX,
    REASON_ALL_CHANNELS_EXHAUSTED,
    REASON_CODES,
    REASON_DUPLICATE_PLAN,
    REASON_NOT_PUBLISHED_BY_AMC,
    REASON_PARSE_IMPOSSIBLE,
    STATUS_MANUAL,
    EscalationQueue,
)
from src.agents.manual_register import (
    RESOLVED_STATUSES,
    ManualRow,
    annotate,
    counts_by_reason,
    for_amc,
    for_queue_id,
    load,
    mark_resolved,
    open_rows,
    pending_rows,
)

QUEUE_JSONL = "escalation_queue.jsonl"
MANUAL_CSV = "manual_intervention.csv"


def _queue(tmp_path: Path) -> EscalationQueue:
    return EscalationQueue(
        queue_path=tmp_path / QUEUE_JSONL,
        manual_csv_path=tmp_path / MANUAL_CSV,
    )


def _register(tmp_path: Path) -> Path:
    return tmp_path / MANUAL_CSV


def _park_direct(
    tmp_path: Path,
    amc: str = "Test AMC",
    scheme: str = "Test Fund",
    month: str = "2026-08",
    reason: str = REASON_NOT_PUBLISHED_BY_AMC,
):
    q = _queue(tmp_path)
    ticket = q.enqueue(amc, scheme, month, "T2", 60.0, "full_portfolio")
    assert ticket is not None
    q.park(ticket, reason)
    return ticket


def _park_exhausted(
    tmp_path: Path,
    amc: str = "Test AMC",
    scheme: str = "Test Fund",
    month: str = "2026-08",
):
    q = _queue(tmp_path)
    ticket = q.enqueue(amc, scheme, month, "T2", 60.0, "full_portfolio")
    assert ticket is not None
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    for channel in fetch_channels[:N_MAX]:
        q.record_attempt(ticket, channel, False)
    assert ticket.status == STATUS_MANUAL
    return ticket


def _full_row(**overrides: str) -> dict[str, str]:
    row = dict.fromkeys(MANUAL_COLUMNS, "")
    row.update(overrides)
    return row


def _raw_rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _statuses(path: Path) -> list[str]:
    return [row["status"] for row in _raw_rows(path)]


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


# ---------------------------------------------------------------------------
# 1. tolerant reader: missing / unreadable file -> [] without raising
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_empty_list_without_raising(tmp_path):
    path = tmp_path / MANUAL_CSV
    assert load(path) == []
    assert pending_rows(path) == []
    assert open_rows(path) == []
    assert for_amc("Test AMC", path) == []
    assert for_queue_id("ESC-nope", path) is None
    assert counts_by_reason(path) == {}


def test_load_never_raises_on_unreadable_path(tmp_path):
    assert load(tmp_path) == []  # a directory, not a CSV


# ---------------------------------------------------------------------------
# 2. a CSV written by the real escalation writer round-trips (§11.4 columns)
# ---------------------------------------------------------------------------

def test_escalation_parked_row_round_trips_with_documented_columns(tmp_path):
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    ticket = _park_exhausted(tmp_path)
    path = _register(tmp_path)
    with open(path, encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == list(MANUAL_COLUMNS)
    rows = load(path)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, ManualRow)
    assert row.queue_id == ticket.queue_id
    assert row.amc == "Test AMC"
    assert row.scheme == "Test Fund"
    assert row.month == "2026-08"
    assert row.coverage_pct == 60.0
    assert row.tier == "T3"
    assert row.document_class == "full_portfolio"
    assert row.channels_tried == tuple(fetch_channels[:N_MAX])
    assert row.attempts == N_MAX
    assert row.reason_code == REASON_ALL_CHANNELS_EXHAUSTED
    assert row.reason_code in REASON_CODES
    assert row.first_seen == ticket.first_seen
    assert row.last_tried == ticket.last_tried
    assert row.assigned_to == ""
    assert row.status == STATUS_MANUAL
    assert row.notes == ""
    assert row.resolved is False
    assert row.resolved_at == ""
    assert row.resolved_by == ""


def test_park_direct_row_round_trips(tmp_path):
    ticket = _park_direct(tmp_path, reason=REASON_PARSE_IMPOSSIBLE)
    row = for_queue_id(ticket.queue_id, _register(tmp_path))
    assert row is not None
    assert row.reason_code == REASON_PARSE_IMPOSSIBLE
    assert row.channels_tried == ()
    assert row.attempts == 0
    assert row.status == STATUS_MANUAL


# ---------------------------------------------------------------------------
# 3. malformed rows are skipped, good rows kept
# ---------------------------------------------------------------------------

def test_load_skips_malformed_rows_and_keeps_good_ones(tmp_path):
    ticket = _park_direct(tmp_path)
    path = _register(tmp_path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ESC-short", "A", "S"])  # too short: no month
        writer.writerow([])  # blank line
        writer.writerow(
            list(_full_row(queue_id="", amc="A", scheme="S", month="2026-08",
                           reason_code=REASON_DUPLICATE_PLAN, status=STATUS_MANUAL).values())
        )  # empty queue_id
        writer.writerow(
            list(_full_row(queue_id="ESC-nan", amc="A", scheme="S", month="2026-08",
                           coverage_pct="not-a-number", status=STATUS_MANUAL).values())
        )  # unparseable coverage
    rows = load(path)
    assert [r.queue_id for r in rows] == [ticket.queue_id]
    assert rows[0].amc == "Test AMC"


def test_load_handles_headerless_file_positionally(tmp_path):
    path = _register(tmp_path)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(
            list(_full_row(queue_id="ESC-hl", amc="A", scheme="S", month="2026-08",
                           status=STATUS_MANUAL).values())
        )
    rows = load(path)
    assert len(rows) == 1
    assert rows[0].queue_id == "ESC-hl"
    assert rows[0].status == STATUS_MANUAL


# ---------------------------------------------------------------------------
# 4. pending_rows / open_rows: only rows a human has NOT resolved
# ---------------------------------------------------------------------------

def test_pending_and_open_rows_return_only_unresolved(tmp_path):
    first = _park_direct(tmp_path, month="2026-08")
    second = _park_direct(tmp_path, amc="Second AMC", scheme="Other Fund", month="2026-09")
    path = _register(tmp_path)
    assert mark_resolved(first.queue_id, status="DONE", assigned_to="alice",
                         notes="filled from AMFI", path=path) is True
    pending = pending_rows(path)
    assert [r.queue_id for r in pending] == [second.queue_id]
    assert open_rows(path) == pending
    assert all(not r.resolved for r in pending)
    assert len(load(path)) == 2
    resolved = for_queue_id(first.queue_id, path)
    assert resolved is not None
    assert resolved.resolved is True


# ---------------------------------------------------------------------------
# 5. for_amc / for_queue_id filters
# ---------------------------------------------------------------------------

def test_for_amc_filters_case_insensitively(tmp_path):
    axis = _park_direct(tmp_path, amc="Axis Mutual Fund", scheme="Fund A", month="2026-08")
    _park_direct(tmp_path, amc="Navi Mutual Fund", scheme="Fund B", month="2026-08")
    path = _register(tmp_path)
    assert [r.queue_id for r in for_amc("Axis Mutual Fund", path)] == [axis.queue_id]
    assert [r.queue_id for r in for_amc("axis mutual fund", path)] == [axis.queue_id]
    assert for_amc("Unknown AMC", path) == []


def test_for_queue_id_returns_the_single_row_or_none(tmp_path):
    navi = _park_direct(tmp_path, amc="Navi Mutual Fund", scheme="Fund B", month="2026-08")
    path = _register(tmp_path)
    row = for_queue_id(navi.queue_id, path)
    assert row is not None
    assert row.scheme == "Fund B"
    assert for_queue_id("ESC-does-not-exist", path) is None


# ---------------------------------------------------------------------------
# 6. counts_by_reason: machine-facing summary over the documented codes
# ---------------------------------------------------------------------------

def test_counts_by_reason_counts_documented_reason_codes(tmp_path):
    _park_direct(tmp_path, month="2026-08", reason=REASON_NOT_PUBLISHED_BY_AMC)
    _park_direct(tmp_path, amc="Second AMC", scheme="Fund B", month="2026-09",
                 reason=REASON_NOT_PUBLISHED_BY_AMC)
    _park_direct(tmp_path, amc="Third AMC", scheme="Fund C", month="2026-10",
                 reason=REASON_ALL_CHANNELS_EXHAUSTED)
    path = _register(tmp_path)
    counts = counts_by_reason(path)
    assert counts == {REASON_NOT_PUBLISHED_BY_AMC: 2, REASON_ALL_CHANNELS_EXHAUSTED: 1}
    assert set(counts) <= set(REASON_CODES)
    assert sum(counts.values()) == len(load(path))
    target = next(r for r in load(path) if r.reason_code == REASON_NOT_PUBLISHED_BY_AMC)
    assert mark_resolved(target.queue_id, status="RESOLVED", assigned_to="bob",
                         notes="ok", path=path) is True
    assert counts_by_reason(path, pending_only=True) == {
        REASON_NOT_PUBLISHED_BY_AMC: 1,
        REASON_ALL_CHANNELS_EXHAUSTED: 1,
    }


# ---------------------------------------------------------------------------
# 7. mark_resolved: sets the resolved status and records WHO/WHEN
# ---------------------------------------------------------------------------

def test_mark_resolved_sets_status_and_records_who_and_when(tmp_path):
    ticket = _park_direct(tmp_path)
    path = _register(tmp_path)
    assert annotate(ticket.queue_id, "agent re-check found nothing", path=path) is True
    assert mark_resolved(ticket.queue_id, status="DONE", assigned_to="alice",
                         notes="downloaded from AMFI portal", by="alice", path=path) is True
    row = for_queue_id(ticket.queue_id, path)
    assert row is not None
    assert row.status == "DONE"
    assert row.resolved is True
    assert row.assigned_to == "alice"
    assert row.resolved_by == "alice"
    assert row.resolved_at  # ISO timestamp recorded
    assert "[resolved " in row.notes
    assert "downloaded from AMFI portal" in row.notes
    assert "agent re-check found nothing" in row.notes  # history preserved
    raw = {r["queue_id"]: r for r in _raw_rows(path)}[ticket.queue_id]
    assert raw["status"] == "DONE"
    assert raw["assigned_to"] == "alice"
    assert "by alice" in raw["notes"]


def test_mark_resolved_accepts_both_documented_resolved_statuses(tmp_path):
    first = _park_direct(tmp_path, month="2026-08")
    second = _park_direct(tmp_path, amc="Second AMC", scheme="Fund B", month="2026-09")
    path = _register(tmp_path)
    assert mark_resolved(first.queue_id, status="DONE", assigned_to="a", notes="", path=path)
    assert mark_resolved(second.queue_id, status="RESOLVED", assigned_to="b", notes="", path=path)
    statuses = {r.queue_id: r.status for r in load(path)}
    assert statuses == {first.queue_id: "DONE", second.queue_id: "RESOLVED"}
    assert set(statuses.values()) <= set(RESOLVED_STATUSES)
    assert pending_rows(path) == []


def test_mark_resolved_rejects_non_resolved_status_without_writing(tmp_path):
    ticket = _park_direct(tmp_path)
    path = _register(tmp_path)
    before = _snapshot(path)
    for bad in ("OPEN", "MANUAL", "CLOSED", "done"):
        with pytest.raises(ValueError):
            mark_resolved(ticket.queue_id, status=bad, assigned_to="x", notes="", path=path)
    assert _snapshot(path) == before
    row = for_queue_id(ticket.queue_id, path)
    assert row is not None
    assert row.status == STATUS_MANUAL


# ---------------------------------------------------------------------------
# 8. mark_resolved on an unknown queue_id fails safely (no row created/changed)
# ---------------------------------------------------------------------------

def test_mark_resolved_unknown_queue_id_fails_safely(tmp_path):
    ticket = _park_direct(tmp_path)
    path = _register(tmp_path)
    before = _snapshot(path)
    assert mark_resolved("ESC-unknown", status="DONE", assigned_to="x", notes="y", path=path) is False
    assert mark_resolved("", status="DONE", assigned_to="x", notes="y", path=path) is False
    assert _snapshot(path) == before
    raw = _raw_rows(path)
    assert len(raw) == 1
    assert raw[0]["queue_id"] == ticket.queue_id


def test_mark_resolved_on_missing_file_creates_nothing(tmp_path):
    path = tmp_path / "absent.csv"
    assert mark_resolved("ESC-x", status="DONE", assigned_to="x", notes="y", path=path) is False
    assert not path.exists()


# ---------------------------------------------------------------------------
# 9. THE invariant: no agent path can change status; only mark_resolved writes it
# ---------------------------------------------------------------------------

def test_public_api_surface_is_exactly_the_documented_one():
    assert set(manual_register.__all__) == {
        "DEFAULT_MANUAL_CSV_PATH",
        "MANUAL_COLUMNS",
        "REASON_CODES",
        "RESOLVED_STATUSES",
        "ManualRow",
        "annotate",
        "counts_by_reason",
        "for_amc",
        "for_queue_id",
        "load",
        "mark_resolved",
        "open_rows",
        "pending_rows",
    }


def test_every_reader_is_byte_identical_and_annotate_only_grows_notes(tmp_path):
    ticket = _park_exhausted(tmp_path)
    path = _register(tmp_path)
    before = _snapshot(path)
    for read in (
        lambda: load(path),
        lambda: pending_rows(path),
        lambda: open_rows(path),
        lambda: for_amc("Test AMC", path),
        lambda: for_queue_id(ticket.queue_id, path),
        lambda: counts_by_reason(path),
        lambda: counts_by_reason(path, pending_only=True),
    ):
        read()
        assert _snapshot(path) == before
    assert annotate(ticket.queue_id, "re-checked archive, still incomplete", path=path) is True
    assert _statuses(path) == [STATUS_MANUAL]  # status column untouched by annotate
    row = for_queue_id(ticket.queue_id, path)
    assert row is not None
    assert row.assigned_to == ""
    after_annotate = _snapshot(path)
    assert after_annotate != before
    assert annotate(ticket.queue_id, "second look, same result", path=path) is True
    row = for_queue_id(ticket.queue_id, path)
    assert row is not None
    assert "re-checked archive, still incomplete" in row.notes
    assert "second look, same result" in row.notes
    assert "[resolved" not in row.notes
    assert mark_resolved(ticket.queue_id, status="RESOLVED", assigned_to="priya",
                         notes="phoned the AMC, got the full sheet", path=path) is True
    assert _statuses(path) == ["RESOLVED"]


def test_resolved_row_is_immutable_to_every_other_public_function(tmp_path):
    ticket = _park_exhausted(tmp_path)
    path = _register(tmp_path)
    assert mark_resolved(ticket.queue_id, status="DONE", assigned_to="alice",
                         notes="human resolved", path=path) is True
    frozen = _snapshot(path)
    assert load(path)
    assert pending_rows(path) == []
    assert open_rows(path) == []
    assert len(for_amc("Test AMC", path)) == 1
    assert for_queue_id(ticket.queue_id, path) is not None
    assert counts_by_reason(path) == {REASON_ALL_CHANNELS_EXHAUSTED: 1}
    assert counts_by_reason(path, pending_only=True) == {}
    assert annotate(ticket.queue_id, "late agent note", path=path) is False
    assert _snapshot(path) == frozen


def test_annotate_fails_safely_on_unknown_id_empty_note_or_missing_file(tmp_path):
    path = tmp_path / MANUAL_CSV
    assert annotate("ESC-nope", "note", path=path) is False
    assert not path.exists()
    ticket = _park_direct(tmp_path)
    before = _snapshot(path)
    assert annotate("ESC-nope", "note", path=path) is False
    assert annotate(ticket.queue_id, "", path=path) is False
    assert _snapshot(path) == before


# ---------------------------------------------------------------------------
# 10. no duplicate rows: same queue_id parked twice / load folds duplicates
# ---------------------------------------------------------------------------

def test_parking_same_queue_id_twice_never_duplicates(tmp_path):
    q = _queue(tmp_path)
    ticket = q.enqueue("Test AMC", "Test Fund", "2026-08", "T2", 60.0, "full_portfolio")
    assert ticket is not None
    q.park(ticket, REASON_NOT_PUBLISHED_BY_AMC)
    q.park(ticket, REASON_NOT_PUBLISHED_BY_AMC)  # already MANUAL: escalation no-op
    q.record_attempt(ticket, "advisorkhoj", False)  # MANUAL ticket: immutable no-op
    path = _register(tmp_path)
    raw = _raw_rows(path)
    assert len(raw) == 1
    rows = load(path)
    assert len(rows) == 1
    assert rows[0].queue_id == ticket.queue_id
    assert rows[0].reason_code == REASON_NOT_PUBLISHED_BY_AMC


def test_load_folds_duplicate_queue_id_rows_last_write_wins(tmp_path):
    path = _register(tmp_path)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANUAL_COLUMNS))
        writer.writeheader()
        writer.writerow(_full_row(queue_id="ESC-dup", amc="A", scheme="S", month="2026-08",
                                  reason_code=REASON_NOT_PUBLISHED_BY_AMC, status=STATUS_MANUAL))
        writer.writerow(_full_row(queue_id="ESC-dup", amc="A", scheme="S", month="2026-08",
                                  reason_code=REASON_PARSE_IMPOSSIBLE, status="DONE",
                                  assigned_to="alice"))
    rows = load(path)
    assert len(rows) == 1
    assert rows[0].reason_code == REASON_PARSE_IMPOSSIBLE
    assert rows[0].status == "DONE"
    assert rows[0].resolved is True


# ---------------------------------------------------------------------------
# never delete rows: a resolve preserves malformed rows verbatim
# ---------------------------------------------------------------------------

def test_mark_resolved_preserves_malformed_rows(tmp_path):
    ticket = _park_direct(tmp_path)
    path = _register(tmp_path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(["ESC-broken", "A", "S"])  # malformed: stays in the file
    assert len(_raw_rows(path)) == 2
    assert mark_resolved(ticket.queue_id, status="DONE", assigned_to="zoe", notes="ok", path=path)
    after = _raw_rows(path)
    assert len(after) == 2  # nothing deleted
    assert after[1]["queue_id"] == "ESC-broken"
    assert after[1]["amc"] == "A"
    assert after[0]["status"] == "DONE"
    assert (after[1]["status"] or "") == ""


# ---------------------------------------------------------------------------
# tmp-path isolation: the real register file is never created or modified
# ---------------------------------------------------------------------------

def test_real_register_file_is_never_created_or_modified(tmp_path):
    def _fingerprint(path):
        p = Path(path)
        if not p.exists():
            return None
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)

    before = _fingerprint(DEFAULT_MANUAL_CSV_PATH)
    ticket = _park_exhausted(tmp_path)
    path = _register(tmp_path)
    assert annotate(ticket.queue_id, "agent note", path=path) is True
    assert mark_resolved(ticket.queue_id, status="DONE", assigned_to="h", notes="ok", path=path)
    assert load(path)
    assert _fingerprint(DEFAULT_MANUAL_CSV_PATH) == before
