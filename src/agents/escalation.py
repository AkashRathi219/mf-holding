"""Append-only escalation queue + manual-intervention register (SPEC §11.3/§11.4, T19).

The integrity agent emits one escalation ticket per T2 scheme-month into
``data/logs/escalation_queue.jsonl``; the owning discovery agent and the two
source agents work the ticket through the fixed §11.3 channel order
(``amc_recheck`` -> ``web_search`` -> ``amfi`` -> ``advisorkhoj`` ->
``manual``) until a full-disclosure parse reaches Σ >= 95% (tier T0) closes
it.  A T1 top-10 fallback is an ACCEPTED outcome and must never enter the
queue: only tier code ``"T2"`` passes :meth:`EscalationQueue.enqueue` (AC-15).

The queue file is append-only: every state change (enqueue, each recorded
channel attempt, parking) appends ONE ``"\\n"``-terminated JSON line and
flushes, so a concurrent reader never observes a partial record.
:meth:`EscalationQueue.load` folds the lines in order keyed by
``(amc, scheme, month)`` with last write wins, so the fold always exposes
exactly one ticket per scheme-month however many snapshots the file holds.
A corrupt or partially written line (crash mid-append) is skipped
defensively, and an append onto a file whose tail lost its newline first
terminates the stale fragment so one bad crash cannot poison the next record.

Dedupe (AC-15): a repeated audit of the same unresolved gap does not stack
tickets - ``enqueue`` is a no-op while the folded ticket for the key is OPEN,
and while it is MANUAL (a parked scheme-month has no further automation until
a human resolves the register row, §5.1/§11.4).  Once the folded ticket is
CLOSED a genuinely new failure may open a fresh ticket.

Attempt cap (AC-16): ``N_max = 4`` attempts per release cycle across all
channels.  A successful attempt closes the ticket; after ``N_max`` failed
attempts - or via an explicit :meth:`EscalationQueue.park` - the ticket
becomes MANUAL and one row is appended to the human-owned register
``data/reference/manual_intervention.csv`` (columns exactly per §11.4, with
``assigned_to``/``notes`` left empty for the human).  A MANUAL ticket is
immutable to agents: no automated call may change its status, append an
attempt or duplicate its register row - the human-set ``status`` column is
authoritative (§11.4, AC-20).

All paths are constructor-injectable so tests and the scheduler can point the
queue at a sandbox; the defaults are the SPEC locations relative to the
``mf_holding`` working directory.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_QUEUE_PATH = Path("data/logs/escalation_queue.jsonl")
DEFAULT_MANUAL_CSV_PATH = Path("data/reference/manual_intervention.csv")

CHANNELS: tuple[str, ...] = ("amc_recheck", "web_search", "amfi", "advisorkhoj", "manual")
CHANNEL_MANUAL = CHANNELS[-1]

N_MAX = 4

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_MANUAL = "MANUAL"
STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_CLOSED, STATUS_MANUAL)

TIER_ESCALATE_CODE = "T2"
TIER_MANUAL_CODE = "T3"

REASON_NOT_PUBLISHED_BY_AMC = "NOT_PUBLISHED_BY_AMC"
REASON_ALL_CHANNELS_EXHAUSTED = "ALL_CHANNELS_EXHAUSTED"
REASON_PARSE_IMPOSSIBLE = "PARSE_IMPOSSIBLE"
REASON_SCHEME_DISCONTINUED = "SCHEME_DISCONTINUED"
REASON_DUPLICATE_PLAN = "DUPLICATE_PLAN"
REASON_CODES: tuple[str, ...] = (
    REASON_NOT_PUBLISHED_BY_AMC,
    REASON_ALL_CHANNELS_EXHAUSTED,
    REASON_PARSE_IMPOSSIBLE,
    REASON_SCHEME_DISCONTINUED,
    REASON_DUPLICATE_PLAN,
)

MANUAL_COLUMNS: tuple[str, ...] = (
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

_CHANNELS_SEPARATOR = "|"


@dataclass
class Ticket:
    """One escalated scheme-month (§11.3 queue record, mutable work state)."""

    queue_id: str
    amc: str
    scheme: str
    month: str
    tier: str
    coverage_pct: float
    document_class: str
    channels_tried: list[str] = field(default_factory=list)
    attempts: int = 0
    first_seen: str = ""
    last_tried: str = ""
    status: str = STATUS_OPEN


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_queue_id() -> str:
    return f"ESC-{uuid.uuid4().hex[:12]}"


def next_channel(ticket: Ticket) -> str:
    """First §11.3 channel not yet tried for ``ticket``; ``"manual"`` once exhausted."""
    for channel in CHANNELS:
        if channel not in ticket.channels_tried:
            return channel
    return CHANNEL_MANUAL


def _ticket_record(ticket: Ticket) -> dict:
    return {
        "queue_id": ticket.queue_id,
        "amc": ticket.amc,
        "scheme": ticket.scheme,
        "month": ticket.month,
        "tier": ticket.tier,
        "coverage_pct": ticket.coverage_pct,
        "document_class": ticket.document_class,
        "channels_tried": list(ticket.channels_tried),
        "attempts": ticket.attempts,
        "first_seen": ticket.first_seen,
        "last_tried": ticket.last_tried,
        "status": ticket.status,
    }


def _ticket_from_record(record: object) -> Ticket | None:
    if not isinstance(record, dict):
        return None
    amc = record.get("amc")
    scheme = record.get("scheme")
    month = record.get("month")
    status = record.get("status", STATUS_OPEN)
    if not amc or not scheme or not month or status not in STATUSES:
        return None
    channels = record.get("channels_tried")
    if not isinstance(channels, list):
        channels = []
    try:
        attempts = int(record.get("attempts") or 0)
        coverage = float(record.get("coverage_pct") or 0.0)
    except (TypeError, ValueError):
        return None
    return Ticket(
        queue_id=str(record.get("queue_id") or ""),
        amc=str(amc),
        scheme=str(scheme),
        month=str(month),
        tier=str(record.get("tier") or ""),
        coverage_pct=coverage,
        document_class=str(record.get("document_class") or ""),
        channels_tried=[str(c) for c in channels],
        attempts=attempts,
        first_seen=str(record.get("first_seen") or ""),
        last_tried=str(record.get("last_tried") or ""),
        status=str(status),
    )


class EscalationQueue:
    """Append-only T2 escalation queue with a last-write-wins fold (§11.3)."""

    def __init__(
        self,
        queue_path: str | Path = DEFAULT_QUEUE_PATH,
        manual_csv_path: str | Path = DEFAULT_MANUAL_CSV_PATH,
    ) -> None:
        self.queue_path = Path(queue_path)
        self.manual_csv_path = Path(manual_csv_path)

    def enqueue(
        self,
        amc: str,
        scheme: str,
        month: str,
        tier: str,
        coverage_pct: float,
        document_class: str,
    ) -> Ticket | None:
        """Append one OPEN ticket for the T2 scheme-month, deduped (AC-15).

        Only tier code ``"T2"`` may enter the queue - a T1 top-10 fallback is
        an accepted outcome and returns ``None`` without touching the file, as
        do T0/T3.  While the folded ticket for ``(amc, scheme, month)`` is
        OPEN or MANUAL the existing ticket is returned unchanged (repeated
        audits never stack tickets; a parked scheme-month has no further
        automation).  After a CLOSED ticket a new failure opens a fresh one.
        """
        if tier != TIER_ESCALATE_CODE:
            return None
        amc, scheme, month = str(amc), str(scheme), str(month)
        if not amc or not scheme or not month:
            raise ValueError("enqueue requires non-empty amc, scheme and month")
        existing = self.load().get((amc, scheme, month))
        if existing is not None and existing.status in (STATUS_OPEN, STATUS_MANUAL):
            return existing
        ticket = Ticket(
            queue_id=_new_queue_id(),
            amc=amc,
            scheme=scheme,
            month=month,
            tier=tier,
            coverage_pct=float(coverage_pct),
            document_class=str(document_class),
            channels_tried=[],
            attempts=0,
            first_seen=_now(),
            last_tried="",
            status=STATUS_OPEN,
        )
        self._append_ticket(ticket)
        return ticket

    def load(self) -> dict[tuple[str, str, str], Ticket]:
        """Fold the queue file in order, last write wins per scheme-month key.

        Unparseable, partial or schema-invalid lines (e.g. a crash mid-append)
        are skipped without raising.
        """
        tickets: dict[tuple[str, str, str], Ticket] = {}
        if not self.queue_path.exists():
            return tickets
        with open(self.queue_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = _ticket_from_record(record)
            if ticket is not None:
                tickets[(ticket.amc, ticket.scheme, ticket.month)] = ticket
        return tickets

    def record_attempt(self, ticket: Ticket, channel: str, success: bool) -> Ticket:
        """Record one channel attempt on an OPEN ticket and persist the snapshot.

        Appends ``channel`` to ``channels_tried`` (once - the list stays an
        ordered subsequence of the §11.3 order), increments ``attempts`` and
        stamps ``last_tried``.  ``success`` closes the ticket (full disclosure
        reached, AC-16); otherwise ``attempts >= N_MAX`` parks it as MANUAL
        with an ``ALL_CHANNELS_EXHAUSTED`` register row.  A ticket that is not
        OPEN - in particular a MANUAL one, which is immutable to agents
        (§11.4) - is returned unchanged with no file writes.
        """
        if ticket.status != STATUS_OPEN:
            return ticket
        if channel not in CHANNELS:
            raise ValueError(f"unknown escalation channel: {channel!r}")
        if channel not in ticket.channels_tried:
            ticket.channels_tried.append(channel)
        ticket.attempts += 1
        ticket.last_tried = _now()
        if success:
            ticket.status = STATUS_CLOSED
        elif ticket.attempts >= N_MAX:
            ticket.status = STATUS_MANUAL
        self._append_ticket(ticket)
        if ticket.status == STATUS_MANUAL:
            self._append_manual_row(ticket, REASON_ALL_CHANNELS_EXHAUSTED)
        return ticket

    def park(self, ticket: Ticket, reason_code: str) -> Ticket:
        """Force ``ticket`` MANUAL and append its manual-register row (§11.4).

        ``reason_code`` must be one of the five §11.4 codes.  Only an OPEN
        ticket can be parked: an already-MANUAL ticket is immutable (the call
        is an idempotent no-op that never duplicates a register row) and a
        CLOSED ticket stays closed.
        """
        if reason_code not in REASON_CODES:
            raise ValueError(f"unknown manual-register reason_code: {reason_code!r}")
        if ticket.status != STATUS_OPEN:
            return ticket
        ticket.status = STATUS_MANUAL
        self._append_ticket(ticket)
        self._append_manual_row(ticket, reason_code)
        return ticket

    def _append_ticket(self, ticket: Ticket) -> None:
        self._append_line(json.dumps(_ticket_record(ticket), ensure_ascii=False) + "\n")

    def _append_line(self, line: str) -> None:
        path = self.queue_path
        path.parent.mkdir(parents=True, exist_ok=True)
        terminated = True
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                terminated = fh.read(1) == b"\n"
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if not terminated:
                fh.write("\n")
            fh.write(line)
            fh.flush()

    def _append_manual_row(self, ticket: Ticket, reason_code: str) -> None:
        if self._register_has(ticket.queue_id):
            return
        path = self.manual_csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        row = {
            "queue_id": ticket.queue_id,
            "amc": ticket.amc,
            "scheme": ticket.scheme,
            "month": ticket.month,
            "coverage_pct": ticket.coverage_pct,
            "tier": TIER_MANUAL_CODE,
            "document_class": ticket.document_class,
            "channels_tried": _CHANNELS_SEPARATOR.join(ticket.channels_tried),
            "attempts": ticket.attempts,
            "reason_code": reason_code,
            "first_seen": ticket.first_seen,
            "last_tried": ticket.last_tried,
            "assigned_to": "",
            "status": STATUS_MANUAL,
            "notes": "",
        }
        with open(path, "a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(MANUAL_COLUMNS))
            if write_header:
                writer.writeheader()
            writer.writerow(row)
            fh.flush()

    def _register_has(self, queue_id: str) -> bool:
        if not self.manual_csv_path.exists():
            return False
        with open(self.manual_csv_path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("queue_id") == queue_id:
                    return True
        return False
