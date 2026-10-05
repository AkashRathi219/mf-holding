"""Integrity-tiers verifier (SPEC §11.0; PLAN T37; AC-13, AC-14, AC-21).

Read-only audit of the AC-21 tier sidecar ``data/reference/integrity_tiers.json``
against the escalation queue, the manual-intervention register and the webapp
DB.  Nothing is written: the sidecar is opened for reading only, the queue is
folded through :meth:`EscalationQueue.load` (read-only), the register is read
with ``csv.DictReader`` and the DB is opened ``mode=ro``.

Invariants checked (one report line each; exit 0 when all hold, 1 when any
invariant is violated, 2 when the sidecar or queue itself cannot be audited):

  1. tier vocabulary        every sidecar ``tier_code`` is in {T0, T1, T2, T3}.
  2. histogram consistency  per-tier counts sum exactly to the number of
                            sidecar entries; the histogram is printed.
  3. T2 => open ticket      every ``escalate=true`` record has an OPEN ticket
                            in the escalation queue keyed
                            ``(amc, scheme, month)`` — the queue's month may
                            be the literal ``"unknown"`` bucket and the
                            sidecar key is ``"<amc>|<fund_name>|<month>"``.
                            The check set is ``escalate=true`` (a superset of
                            tier T2) so a suspect-scale record the queue's
                            T2-only gate refused is surfaced too, per §11.1
                            "always escalated".
  4. T3 => manual row       every T3 record has a row in the manual register
                            matched by the ``queue_id`` of its MANUAL ticket.
  5. T1 never escalated     no ``tier_code == "T1"`` record may carry
                            ``escalate=true`` (false-positive-storm guard).
  6. sidecar covers schemes the number of sidecar entries equals the number of
                            ``schemes`` rows in the DB opened READ-ONLY and
                            every displayed scheme's key is present in the
                            sidecar.  An unavailable DB yields an explicit
                            SKIP, not a failure.

CLI overrides exist so the verifier can run against test fixtures; the
defaults are the real ``data/`` paths anchored at the repository root,
independent of the working directory:

    python scripts/verify_integrity_tiers.py
    python scripts/verify_integrity_tiers.py --tiers <sidecar.json> --queue <queue.jsonl> --manual <register.csv> --db <webapp.db>
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents.escalation import (  # noqa: E402
    DEFAULT_MANUAL_CSV_PATH,
    MANUAL_COLUMNS,
    STATUS_OPEN,
    TIER_ESCALATE_CODE,
    TIER_MANUAL_CODE,
    EscalationQueue,
)
from src.agents.tier_lookup import (  # noqa: E402
    TIER_CODE_COMPLETE_100,
    TIER_CODE_ESCALATE,
    TIER_CODE_MANUAL,
    TIER_CODE_TOP10_FALLBACK,
    load_tiers,
    tier_key,
)

DEFAULT_TIERS_PATH = ROOT / "data" / "reference" / "integrity_tiers.json"
DEFAULT_QUEUE_PATH = ROOT / "data" / "logs" / "escalation_queue.jsonl"
DEFAULT_DB_PATH = ROOT / "data" / "webapp.db"
DEFAULT_MANUAL_PATH = ROOT / DEFAULT_MANUAL_CSV_PATH

VALID_TIER_CODES = (
    TIER_CODE_COMPLETE_100,
    TIER_CODE_TOP10_FALLBACK,
    TIER_CODE_ESCALATE,
    TIER_CODE_MANUAL,
)

STATUS_OK = "OK"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

MAX_EXAMPLES = 5
N_CHECKS = 6


@dataclass
class CheckResult:
    num: int
    title: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)
    count: int = 0


def _split_key(key: str) -> tuple[str, str, str] | None:
    parts = key.split("|", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _check_vocabulary(raw: dict) -> CheckResult:
    bad: list[str] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            bad.append(f"{key} -> record is {type(value).__name__}, not a JSON object")
            continue
        code = value.get("tier_code")
        if code not in VALID_TIER_CODES:
            bad.append(f"{key} -> tier_code {code!r} not in {{{', '.join(VALID_TIER_CODES)}}}")
    if bad:
        return CheckResult(1, "tier vocabulary", STATUS_FAIL,
                           f"{len(bad)} record(s) with an invalid or missing tier_code", bad)
    return CheckResult(1, "tier vocabulary", STATUS_OK,
                       f"all {len(raw)} record(s) carry tier_code in {{{', '.join(VALID_TIER_CODES)}}}")


def _check_histogram(raw: dict) -> CheckResult:
    hist: Counter = Counter()
    for value in raw.values():
        if isinstance(value, dict) and value.get("tier_code") in VALID_TIER_CODES:
            hist[value["tier_code"]] += 1
    accounted = sum(hist.values()) == len(raw)
    histogram_line = " ".join(f"{code}={hist.get(code, 0)}" for code in VALID_TIER_CODES)
    details = [f"histogram: {histogram_line}"]
    unclassified = len(raw) - sum(hist.values())
    if unclassified:
        details.append(f"unclassified (invalid/missing tier_code): {unclassified}")
    if accounted:
        return CheckResult(2, "histogram consistency", STATUS_OK,
                           f"per-tier counts sum to {len(raw)} sidecar entries", details)
    return CheckResult(2, "histogram consistency", STATUS_FAIL,
                       f"per-tier counts sum to {sum(hist.values())}, expected {len(raw)} sidecar entries",
                       details)


def _check_t2_tickets(tiers: dict, queue: dict) -> CheckResult:
    escalated = 0
    t2 = 0
    orphans: list[str] = []
    for key, record in tiers.items():
        if not record.get("escalate"):
            continue
        escalated += 1
        if record.get("tier_code") == TIER_ESCALATE_CODE:
            t2 += 1
        parts = _split_key(key)
        if parts is None:
            orphans.append(f"{key} -> malformed sidecar key, expected '<amc>|<fund>|<month>'")
            continue
        amc, fund, month = parts
        ticket = queue.get((amc, fund, month))
        if ticket is None:
            orphans.append(f"{key} -> no ticket for queue key ('{amc}', '{fund}', '{month}')")
        elif ticket.status != STATUS_OPEN:
            orphans.append(f"{key} -> ticket {ticket.queue_id} is {ticket.status}, not OPEN")
    if orphans:
        return CheckResult(3, "T2 => open ticket", STATUS_FAIL,
                           f"{len(orphans)} orphan(s) among {escalated} escalate=true record(s)",
                           orphans, count=len(orphans))
    return CheckResult(3, "T2 => open ticket", STATUS_OK,
                       f"{escalated} escalate=true record(s) ({t2} tier_code={TIER_ESCALATE_CODE}), "
                       f"{len(orphans)} orphan(s)", count=len(orphans))


def _check_t3_manual(
    tiers: dict,
    queue: dict,
    manual_path: Path,
    manual_rows: list[dict] | None,
    manual_fields: list[str] | None,
    manual_error: str,
) -> CheckResult:
    t3_keys = [key for key, record in tiers.items() if record.get("tier_code") == TIER_MANUAL_CODE]
    details: list[str] = []
    if manual_rows is None:
        details.append(f"register unreadable: {manual_error}" if manual_error
                       else f"register absent: {manual_path}")
    elif manual_fields is not None and manual_fields != list(MANUAL_COLUMNS):
        details.append(f"register header deviates from MANUAL_COLUMNS: {manual_fields}")
    register_ids: set[str] = set()
    if manual_rows is not None:
        for row in manual_rows:
            queue_id = str(row.get("queue_id") or "").strip()
            if queue_id:
                register_ids.add(queue_id)
    missing: list[str] = []
    for key in t3_keys:
        parts = _split_key(key)
        if parts is None:
            missing.append(f"{key} -> malformed sidecar key, expected '<amc>|<fund>|<month>'")
            continue
        amc, fund, month = parts
        ticket = queue.get((amc, fund, month))
        queue_id = ticket.queue_id if ticket is not None else ""
        if not queue_id:
            missing.append(f"{key} -> no MANUAL ticket in the queue to source queue_id from")
        elif queue_id not in register_ids:
            missing.append(f"{key} -> queue_id {queue_id} has no row in the register")
    if missing:
        return CheckResult(4, "T3 => manual row", STATUS_FAIL,
                           f"{len(missing)} of {len(t3_keys)} T3 record(s) missing a register row",
                           details + missing, count=len(missing))
    if not t3_keys:
        state = "absent" if manual_rows is None else f"{len(manual_rows)} row(s)"
        return CheckResult(4, "T3 => manual row", STATUS_OK,
                           f"0 T3 record(s), nothing to match (register {state})", details)
    return CheckResult(4, "T3 => manual row", STATUS_OK,
                       f"{len(t3_keys)} T3 record(s) matched by queue_id, 0 missing", details)


def _check_t1_never_escalated(tiers: dict) -> CheckResult:
    bad = [
        f"{key} -> tier_code={TIER_CODE_TOP10_FALLBACK} with escalate={record.get('escalate')!r}"
        for key, record in tiers.items()
        if record.get("tier_code") == TIER_CODE_TOP10_FALLBACK and record.get("escalate")
    ]
    if bad:
        return CheckResult(5, "T1 never escalated", STATUS_FAIL,
                           f"{len(bad)} T1 record(s) with escalate=true (false-positive-storm guard)",
                           bad, count=len(bad))
    return CheckResult(5, "T1 never escalated", STATUS_OK, "0 violation(s)", count=0)


def _read_db(db_path: Path) -> tuple[list[tuple] | None, str]:
    if not db_path.exists():
        return None, f"file not found: {db_path}"
    uri = "file:" + quote(str(db_path).replace("\\", "/")) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return None, f"sqlite connect failed ({exc})"
    try:
        return con.execute("SELECT amc, fund_name, as_of FROM schemes").fetchall(), ""
    except sqlite3.Error as exc:
        return None, f"schemes table unreadable ({exc})"
    finally:
        con.close()


def _check_db_coverage(raw: dict, db_rows: list[tuple] | None, db_error: str) -> CheckResult:
    if db_rows is None:
        return CheckResult(6, "sidecar covers schemes", STATUS_SKIP,
                           f"DB unavailable ({db_error}) - check skipped")
    db_keys = [tier_key(amc, fund, as_of) for amc, fund, as_of in db_rows]
    missing = sorted({key for key in db_keys if key not in raw})
    extra = sorted(set(raw) - set(db_keys))
    details: list[str] = [f"displayed scheme missing from sidecar: {key}" for key in missing]
    if extra:
        details.append(f"informational: {len(extra)} sidecar key(s) not present in the DB")
    counts_equal = len(raw) == len(db_rows)
    if counts_equal and not missing:
        return CheckResult(6, "sidecar covers schemes", STATUS_OK,
                           f"{len(raw)} sidecar entries == {len(db_rows)} schemes row(s); "
                           f"0 displayed scheme(s) missing from sidecar")
    bits = []
    if not counts_equal:
        bits.append(f"{len(raw)} sidecar entries != {len(db_rows)} schemes row(s)")
    if missing:
        bits.append(f"{len(missing)} displayed scheme(s) missing from the sidecar")
    return CheckResult(6, "sidecar covers schemes", STATUS_FAIL, "; ".join(bits), details)


def _read_manual_csv(path: Path) -> tuple[list[dict] | None, list[str] | None, str]:
    if not path.exists():
        return None, None, ""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = [dict(row) for row in reader]
            fields = list(reader.fieldnames or [])
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        return None, None, str(exc)
    return rows, fields, ""


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the integrity-tier sidecar (SPEC §11.0, PLAN T37).")
    parser.add_argument("--tiers", default=str(DEFAULT_TIERS_PATH),
                        help="tier sidecar JSON (default: data/reference/integrity_tiers.json)")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE_PATH),
                        help="escalation queue JSONL (default: data/logs/escalation_queue.jsonl)")
    parser.add_argument("--manual", default=str(DEFAULT_MANUAL_PATH),
                        help="manual-intervention register CSV (default: data/reference/manual_intervention.csv)")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH),
                        help="webapp SQLite DB opened read-only (default: data/webapp.db)")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    tiers_path = Path(args.tiers)
    if not tiers_path.exists():
        print(f"FATAL: tier sidecar not found: {tiers_path}")
        return 2
    try:
        with open(tiers_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(f"FATAL: tier sidecar unreadable: {tiers_path} ({exc})")
        return 2
    if not isinstance(raw, dict):
        print(f"FATAL: tier sidecar is not a JSON object: {tiers_path}")
        return 2
    tiers = load_tiers(tiers_path)

    try:
        queue = EscalationQueue(queue_path=args.queue).load()
    except OSError as exc:
        print(f"FATAL: escalation queue unreadable: {args.queue} ({exc})")
        return 2

    manual_rows, manual_fields, manual_error = _read_manual_csv(Path(args.manual))
    db_rows, db_error = _read_db(Path(args.db))

    results = [
        _check_vocabulary(raw),
        _check_histogram(raw),
        _check_t2_tickets(tiers, queue),
        _check_t3_manual(tiers, queue, Path(args.manual), manual_rows, manual_fields, manual_error),
        _check_t1_never_escalated(tiers),
        _check_db_coverage(raw, db_rows, db_error),
    ]

    print("== integrity-tiers verifier ==")
    print(f"sidecar : {_display(tiers_path)} ({len(raw)} entries)")
    print(f"queue   : {_display(Path(args.queue))} ({len(queue)} folded ticket(s))")
    manual_state = "absent" if manual_rows is None else f"{len(manual_rows)} row(s)"
    print(f"manual  : {_display(Path(args.manual))} ({manual_state})")
    db_state = "available" if db_rows is not None else "unavailable"
    print(f"db      : {_display(Path(args.db))} ({db_state})")

    failed: list[str] = []
    for result in results:
        print(f"[{result.num}/{N_CHECKS}] {result.title:<24}: {result.status} - {result.summary}")
        for line in result.details[:MAX_EXAMPLES]:
            print(f"       {line}")
        if len(result.details) > MAX_EXAMPLES:
            print(f"       ... and {len(result.details) - MAX_EXAMPLES} more")
        if result.status == STATUS_FAIL:
            failed.append(f"[{result.num}/{N_CHECKS}] {result.title}")

    if failed:
        print(f"RESULT: FAIL - violated: {', '.join(failed)}")
        return 1
    print(f"RESULT: PASS - {results[2].count} orphans, {results[3].count} missing manual rows, "
          f"{results[4].count} T1 escalations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
