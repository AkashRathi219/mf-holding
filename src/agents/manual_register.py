"""Human-owned manual-intervention register reader/resolver (SPEC §11.4, T35).

``data/reference/manual_intervention.csv`` is APPENDED by
:mod:`src.agents.escalation` (``EscalationQueue.park`` and ``record_attempt``
at the ``N_max`` cap) and is owned by humans from that moment on (OQ-11):
agents may read it and append timestamped annotations, but the ONLY writer of
a resolved ``status`` is :func:`mark_resolved` - the documented human entry
point.  A human-set ``status=DONE|RESOLVED`` is authoritative (AC-20): the row
is closed to agents, nothing re-opens it, nothing auto-closes it, and no
automated path can flip it back.

The module structure enforces that invariant:

* :func:`load`, :func:`pending_rows`/:func:`open_rows`, :func:`for_amc`,
  :func:`for_queue_id` and :func:`counts_by_reason` are pure readers - they
  never create, modify or delete the file, skip malformed rows defensively and
  never raise (a missing file reads as no rows).
* :func:`annotate` is the only agent-facing write: it appends
  ``[<ts> by <actor>] <note>`` to the ``notes`` column of an UNRESOLVED row
  and refuses resolved rows and unknown ``queue_id`` values.  It cannot touch
  ``status``.
* :func:`mark_resolved` is the human entry point and the only function that
  may set a resolved status.  It accepts only ``RESOLVED_STATUSES`` (so it can
  never un-resolve a row), refuses unknown ``queue_id`` values (returns
  ``False``, creating nothing), and records WHO/WHEN: the resolver into
  ``assigned_to`` plus a ``[resolved <ts> by <actor>]`` annotation appended to
  ``notes`` (parsed back out as :attr:`ManualRow.resolved_at` /
  :attr:`ManualRow.resolved_by`).

Rows are never deleted and history is never rewritten: a resolve/annotate
rewrites the file with every original row intact (malformed ones included),
updating only the target row's ``status``/``assigned_to``/``notes`` cells.
:func:`load` folds duplicate ``queue_id`` rows last-write-wins, so a row
already appended by ``escalation.park()`` is never duplicated.  The schema -
columns, reason codes, default path - is reused from
:mod:`src.agents.escalation` as the single source of truth; this module
defines no second schema and no row-creating API.  Stdlib only.
"""

from __future__ import annotations

import contextlib
import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.agents.escalation import (
    DEFAULT_MANUAL_CSV_PATH,
    MANUAL_COLUMNS,
    REASON_CODES,
    STATUS_MANUAL,
)

__all__ = [
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
]

RESOLVED_STATUSES: frozenset[str] = frozenset({"DONE", "RESOLVED"})

_RESOLVED_MARKER = "[resolved "
_NOTE_SEPARATOR = "; "


@dataclass(frozen=True)
class ManualRow:
    """One parked scheme-month: the §11.4 columns as a read-only view.

    ``channels_tried`` is stored pipe-separated in the CSV (the escalation
    writer's separator) and exposed here as a tuple in tried order.
    """

    queue_id: str
    amc: str
    scheme: str
    month: str
    coverage_pct: float
    tier: str
    document_class: str
    channels_tried: tuple[str, ...]
    attempts: int
    reason_code: str
    first_seen: str
    last_tried: str
    assigned_to: str
    status: str
    notes: str

    @property
    def resolved(self) -> bool:
        """True once a human set ``status`` to a resolved value (authoritative)."""
        return self.status in RESOLVED_STATUSES

    @property
    def resolved_at(self) -> str:
        """ISO timestamp written by :func:`mark_resolved`; "" before a human resolves."""
        return self._resolved_annotation()[0]

    @property
    def resolved_by(self) -> str:
        """Actor recorded by :func:`mark_resolved`; "" before a human resolves."""
        return self._resolved_annotation()[1]

    def _resolved_annotation(self) -> tuple[str, str]:
        start = self.notes.rfind(_RESOLVED_MARKER)
        if start < 0:
            return "", ""
        end = self.notes.find("]", start)
        if end < 0:
            return "", ""
        body = self.notes[start + len(_RESOLVED_MARKER):end].strip()
        when, sep, who = body.partition(" by ")
        if not sep:
            return "", ""
        return when.strip(), who.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _read_raw(path: str | Path) -> list[list[str]]:
    """Every CSV row as a raw cell list; [] when the file is missing/unreadable."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.reader(fh))
    except (OSError, TypeError, ValueError, csv.Error):
        return []


def _split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Split raw rows into (header, data); a headerless file is read positionally."""
    if rows and "queue_id" in rows[0]:
        return rows[0], rows[1:]
    return list(MANUAL_COLUMNS), rows


def _row_from_cells(header: list[str], cells: list[str]) -> ManualRow | None:
    """Build a :class:`ManualRow`; ``None`` for a structurally malformed row."""
    record = dict(zip(header, cells))
    queue_id = _clean(record.get("queue_id"))
    amc = _clean(record.get("amc"))
    scheme = _clean(record.get("scheme"))
    month = _clean(record.get("month"))
    if not queue_id or not amc or not scheme or not month:
        return None
    try:
        attempts = int(_clean(record.get("attempts")) or "0")
        coverage_pct = float(_clean(record.get("coverage_pct")) or "0")
    except ValueError:
        return None
    channels = tuple(
        part.strip() for part in _clean(record.get("channels_tried")).split("|") if part.strip()
    )
    return ManualRow(
        queue_id=queue_id,
        amc=amc,
        scheme=scheme,
        month=month,
        coverage_pct=coverage_pct,
        tier=_clean(record.get("tier")),
        document_class=_clean(record.get("document_class")),
        channels_tried=channels,
        attempts=attempts,
        reason_code=_clean(record.get("reason_code")),
        first_seen=_clean(record.get("first_seen")),
        last_tried=_clean(record.get("last_tried")),
        assigned_to=_clean(record.get("assigned_to")),
        status=_clean(record.get("status")) or STATUS_MANUAL,
        notes=_clean(record.get("notes")),
    )


def load(path: str | Path = DEFAULT_MANUAL_CSV_PATH) -> list[ManualRow]:
    """Read the register: missing file -> [], malformed rows skipped, never raises.

    Duplicate ``queue_id`` rows fold last-write-wins (so a row already appended
    by ``escalation.park()`` is never duplicated), keeping first-appearance
    order.  Rows are only ever read - the file is never created or modified.
    """
    rows = _read_raw(path)
    if not rows:
        return []
    header, data = _split_header(rows)
    folded: dict[str, ManualRow] = {}
    for cells in data:
        row = _row_from_cells(header, cells)
        if row is not None:
            folded[row.queue_id] = row
    return list(folded.values())


def pending_rows(path: str | Path = DEFAULT_MANUAL_CSV_PATH) -> list[ManualRow]:
    """Rows a human has NOT yet resolved (``status`` outside ``RESOLVED_STATUSES``)."""
    return [row for row in load(path) if not row.resolved]


def open_rows(path: str | Path = DEFAULT_MANUAL_CSV_PATH) -> list[ManualRow]:
    """Alias of :func:`pending_rows` (§11.4 "open" = awaiting a human)."""
    return pending_rows(path)


def for_amc(amc: str, path: str | Path = DEFAULT_MANUAL_CSV_PATH) -> list[ManualRow]:
    """All register rows for ``amc`` (case-insensitive exact name match)."""
    needle = _clean(amc).lower()
    return [row for row in load(path) if row.amc.lower() == needle]


def for_queue_id(queue_id: str, path: str | Path = DEFAULT_MANUAL_CSV_PATH) -> ManualRow | None:
    """The register row for ``queue_id`` (last write wins), or ``None``."""
    wanted = _clean(queue_id)
    for row in load(path):
        if row.queue_id == wanted:
            return row
    return None


def counts_by_reason(
    path: str | Path = DEFAULT_MANUAL_CSV_PATH,
    *,
    pending_only: bool = False,
) -> dict[str, int]:
    """Machine-facing summary for the coordinator: row count per ``reason_code``.

    Counts every loaded row (or, with ``pending_only=True``, only the rows a
    human has not resolved yet).  Codes are the §11.4 set from
    :data:`src.agents.escalation.REASON_CODES`.
    """
    rows = pending_rows(path) if pending_only else load(path)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.reason_code] = counts.get(row.reason_code, 0) + 1
    return counts


def _append_note(existing: str, annotation: str) -> str:
    if not annotation:
        return existing
    if not existing.strip():
        return annotation
    return f"{existing.rstrip()}{_NOTE_SEPARATOR}{annotation}"


def _apply_resolution(
    cells: list[str],
    idx: tuple[int, int, int],
    *,
    status: str,
    assigned_to: str,
    annotation: str,
) -> None:
    s_idx, a_idx, n_idx = idx
    cells[s_idx] = status
    cells[a_idx] = assigned_to
    cells[n_idx] = _append_note(cells[n_idx], annotation)


def _apply_annotation(cells: list[str], idx: tuple[int, int, int], annotation: str) -> None:
    n_idx = idx[2]
    cells[n_idx] = _append_note(cells[n_idx], annotation)


def _rewrite(path: Path, header: list[str], data: list[list[str]]) -> bool:
    """Rewrite the register preserving every row (tmp file, then ``os.replace``)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        return True
    except OSError:
        return False


def _mutate(path: str | Path, queue_id: str, apply, guard=None) -> bool:
    """Apply ``apply(cells, idx)`` to every data row whose ``queue_id`` matches.

    ``idx`` is the ``(status, assigned_to, notes)`` column indices; ``guard``
    may veto a row (the row is then left untouched).  Returns ``False`` - with
    no write and no row created - when the register is missing, unreadable,
    headerless, or holds no matching (and non-vetoed) row.  Every other row,
    malformed ones included, is preserved verbatim.
    """
    wanted = _clean(queue_id)
    if not wanted:
        return False
    rows = _read_raw(path)
    if not rows:
        return False
    header, data = _split_header(rows)
    try:
        q_idx = header.index("queue_id")
        s_idx = header.index("status")
        a_idx = header.index("assigned_to")
        n_idx = header.index("notes")
    except ValueError:
        return False
    matched = False
    for cells in data:
        if len(cells) <= q_idx or cells[q_idx].strip() != wanted:
            continue
        if guard is not None and not guard(cells, (s_idx, a_idx, n_idx)):
            continue
        while len(cells) < len(header):
            cells.append("")
        apply(cells, (s_idx, a_idx, n_idx))
        matched = True
    if not matched:
        return False
    return _rewrite(Path(path), header, data)


def annotate(
    queue_id: str,
    note: str,
    *,
    by: str = "agent",
    path: str | Path = DEFAULT_MANUAL_CSV_PATH,
) -> bool:
    """Append-only agent annotation on an UNRESOLVED row; never touches status.

    Appends ``[<ts> by <by>] <note>`` to the row's ``notes`` column.  Returns
    ``False`` - writing nothing - for an unknown ``queue_id``, an empty note,
    or a row a human already resolved (a resolved row is closed to agents,
    §11.4/AC-20).  This is the ONLY agent-facing write in this module.
    """
    text = _clean(note)
    if not text:
        return False
    annotation = f"[{_now()} by {_clean(by) or 'agent'}] {text}"

    def apply(cells: list[str], idx: tuple[int, int, int]) -> None:
        _apply_annotation(cells, idx, annotation)

    def guard(cells: list[str], idx: tuple[int, int, int]) -> bool:
        return cells[idx[0]].strip() not in RESOLVED_STATUSES

    return _mutate(path, queue_id, apply, guard=guard)


def mark_resolved(
    queue_id: str,
    *,
    status: str,
    assigned_to: str,
    notes: str,
    by: str = "human",
    path: str | Path = DEFAULT_MANUAL_CSV_PATH,
) -> bool:
    """THE human entry point: the only writer of a resolved status (§11.4, AC-20).

    Sets ``status`` - which must be one of ``RESOLVED_STATUSES`` (``DONE`` or
    ``RESOLVED``, else ``ValueError``) - records the resolver in
    ``assigned_to`` and appends ``[resolved <ts> by <by>] <notes>`` to the
    ``notes`` column (WHO/WHEN, parsed back as ``ManualRow.resolved_at`` /
    ``resolved_by``).  An unknown ``queue_id`` returns ``False`` with no row
    created and no file touched; every other row - malformed ones included -
    is preserved verbatim.  Because only resolved statuses are accepted here
    and no other function writes ``status``, no agent code path can auto-close
    or re-open a MANUAL row.
    """
    wanted = _clean(status)
    if wanted not in RESOLVED_STATUSES:
        raise ValueError(
            f"mark_resolved only sets a resolved status {sorted(RESOLVED_STATUSES)}, got {status!r}"
        )
    annotation = f"[resolved {_now()} by {_clean(by) or 'human'}] {_clean(notes)}".rstrip()

    def apply(cells: list[str], idx: tuple[int, int, int]) -> None:
        _apply_resolution(
            cells, idx, status=wanted, assigned_to=_clean(assigned_to), annotation=annotation
        )

    return _mutate(path, queue_id, apply)
