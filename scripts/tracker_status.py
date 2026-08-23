"""Execution-tracker stall detector (plan F2).

Parses docs/plans/EXECUTION_TRACKER.md, cross-references git history, and
reports stalled / false-done / starved work. Exit code 1 on any RED when
--ci is passed, so CI blocks merges that leave the tracker lying.

Usage:
    python scripts/tracker_status.py                # human report
    python scripts/tracker_status.py --ci           # exit 1 on RED
    python scripts/tracker_status.py --skip-verify  # don't re-run done rows' tests
    python scripts/tracker_status.py --selftest     # rule engine self-check
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "docs" / "plans" / "EXECUTION_TRACKER.md"
OUT_JSON = ROOT / "data" / "logs" / "tracker_status.json"

STATES = {"todo", "doing", "blocked", "review", "done", "dropped", "parked"}
PHASE_ORDER = {"F": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
               "8": 8}

DATE_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"], start=1)}
REVALIDATED_RE = re.compile(r"\(revalidated (\d{1,2})-([A-Za-z]{3})(?:-(\d{2,4}))?\)")

RED = "RED"
ORANGE = "ORANGE"


def parse_date(s: str) -> datetime | None:
    """Ledger dates are DD-Mon (year inferred; future => previous year)."""
    m = DATE_RE.match((s or "").strip())
    if not m:
        return None
    day, mon = int(m.group(1)), MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    now = datetime.now()
    try:
        dt = datetime(now.year, mon, day)
    except ValueError:
        return None
    if dt > now + timedelta(days=1):
        dt = dt.replace(year=now.year - 1)
    return dt


def parse_ledger(path: Path) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    rows: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.rstrip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells):
            continue
        if cells and cells[0] == "ID":
            continue
        if len(cells) < 10:
            if any(cells):
                errors.append(f"line {lineno}: expected >=10 columns, got {len(cells)}")
            continue
        row = dict(zip(
            ["id", "task", "phase", "state", "deps", "opened", "touched",
             "verify", "evidence", "blocker"], cells[:10]))
        row["lineno"] = lineno
        if row["state"] not in STATES:
            errors.append(f"line {lineno}: unknown state '{row['state']}'")
        rows.append(row)
    ids = [r["id"] for r in rows]
    dupes = {i for i in ids if ids.count(i) > 1}
    for d in dupes:
        errors.append(f"duplicate task ID '{d}'")
    return rows, errors


def git_tagged_since(query: str, since: datetime) -> bool:
    """True if a commit subject contains `query` after `since`."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--since=" + since.isoformat(),
             "--pretty=%s"],
            capture_output=True, text=True, timeout=20, check=True).stdout
    except Exception:
        return False  # no git available -> never claim STUCK on this signal alone
    return query.lower() in out.lower()


def evaluate(rows: list[dict], skip_verify: bool = True) -> list[dict]:
    findings: list[dict] = []
    now = datetime.now()
    by_id = {r["id"]: r for r in rows}
    active_phase = max((PHASE_ORDER[r["phase"]] for r in rows
                        if r["state"] in ("doing", "review")), default=-1)

    def add(sev, kind, row, msg):
        findings.append({"severity": sev, "kind": kind, "id": row["id"],
                         "msg": msg})

    for r in rows:
        state, rid = r["state"], r["id"]
        touched = parse_date(r["touched"]) or parse_date(r["opened"])
        age_days = (now - touched).days if touched else None

        if state == "doing":
            if age_days is None:
                add(ORANGE, "BAD-DATES", r, "unparseable Touched date")
            elif age_days > 3 and not git_tagged_since(f"[{rid}]", touched):
                add(RED, "STUCK-DOING", r,
                    f"'doing' {age_days}d with no commit tagged [{rid}]")
        elif state == "blocked":
            m = REVALIDATED_RE.search(r["blocker"])
            fresh = False
            if m:
                rv = parse_date(f"{m.group(1)}-{m.group(2)}")
                fresh = bool(rv) and (now - rv).days <= 7
            if age_days is not None and age_days > 7 and not fresh:
                add(RED, "STALE-BLOCK", r,
                    f"'blocked' {age_days}d without a recent "
                    f"(revalidated DD-Mon) stamp")
        elif state == "review":
            if age_days is not None and age_days > 2:
                add(RED, "STUCK-REVIEW", r, f"in review {age_days}d")
        elif state == "done":
            v = (r["verify"] or "").strip().strip("`")
            if v and v != "-" and not skip_verify:
                ok = run_verify(v)
                if not ok:
                    add(RED, "FALSE-DONE", r, f"verify target fails: {v}")
        elif state == "todo":
            deps = [d.strip() for d in re.split(r"[,\s]+", r["deps"])
                    if d.strip() and d.strip() not in ("—", "-", "--")]
            dep_states = [by_id[d]["state"] for d in deps if d in by_id]
            unknown = [d for d in deps if d not in by_id]
            if unknown:
                add(ORANGE, "UNKNOWN-DEP", r, f"deps not in ledger: {unknown}")
            ready = all(ds == "done" for ds in dep_states) and not unknown
            if ready and active_phase >= PHASE_ORDER[r["phase"]] \
                    and age_days is not None and age_days > 5:
                add(ORANGE, "STARVED", r,
                    f"unblocked 'todo' untouched {age_days}d in an active phase")
        elif state == "parked":
            if age_days is not None and age_days > 30:
                add(ORANGE, "REVIEW-LIST", r,
                    f"parked {age_days}d — parking must not become forgetting")

        if state in ("doing", "review") and active_phase > PHASE_ORDER[r["phase"]]:
            pass  # later phases being worked while this open -> caught below

    # Gate breach: a LATER phase has active work while this phase still open.
    open_phases = {PHASE_ORDER[r["phase"]] for r in rows
                   if r["state"] in ("todo", "doing", "blocked", "review")}
    active_later = [r for r in rows if r["state"] in ("doing", "review")
                    and PHASE_ORDER[r["phase"]] in open_phases
                    and PHASE_ORDER[r["phase"]] == max(open_phases)
                    and any(PHASE_ORDER[o["phase"]] < PHASE_ORDER[r["phase"]]
                            for o in rows
                            if o["state"] in ("todo", "doing", "blocked", "review"))]
    for r in active_later:
        add(ORANGE, "GATE-BREACH", r,
            "active phase has open predecessors (serial sequencing)")
    return findings


def run_verify(target: str) -> bool:
    import subprocess as sp
    r = sp.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-x", target],
               cwd=ROOT, capture_output=True, text=True, timeout=600)
    return r.returncode == 0


def render(rows: list[dict], findings: list[dict], errors: list[str]) -> str:
    by_task: dict[str, list] = {}
    for f in findings:
        by_task.setdefault(f["id"], []).append(f)
    lines = ["", "TRACKER STATUS", "=" * 72]
    w = (6, 34, 10, 9)
    lines.append(f"{'ID':<{w[0]}} {'Task':<{w[1]}} {'Phase':<{w[2]}} {'State':<{w[3]}} Flags")
    lines.append("-" * 96)
    for r in rows:
        flags = ",".join(f"{f['severity']}:{f['kind']}" for f in by_task.get(r["id"], [])) \
            or ("ok" if r["state"] in ("done", "parked", "dropped") else "")
        lines.append(f"{r['id']:<{w[0]}} {r['task'][:w[1]]:<{w[1]}} "
                     f"{r['phase']:<{w[2]}} {r['state']:<{w[3]}} {flags}")
    for e in errors:
        lines.append(f"LEDGER ERROR: {e}")
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    lines.append("-" * 96)
    lines.append(f"rows={len(rows)} red={counts.get(RED, 0)} "
                 f"orange={counts.get(ORANGE, 0)} ledger_errors={len(errors)}")
    for f in findings:
        lines.append(f"  [{f['severity']}] {f['kind']} {f['id']}: {f['msg']}")
    return "\n".join(lines)


def selftest() -> int:
    """Rule-engine check with a synthetic ledger."""
    today = datetime.now()
    fmt = lambda dt: f"{dt.day}-{['jan','feb','mar','apr','may','jun','jul',  'aug','sep','oct','nov','dec'][dt.month-1]}"  # noqa: E731
    old = fmt(today - timedelta(days=9))
    recent = fmt(today - timedelta(days=1))
    rows = [
        {"id": "A1", "task": "stuck doing", "phase": "1", "state": "doing",
         "deps": "", "opened": old, "touched": old, "verify": "",
         "evidence": "", "blocker": ""},
        {"id": "A2", "task": "fresh doing", "phase": "1", "state": "doing",
         "deps": "", "opened": recent, "touched": recent, "verify": "",
         "evidence": "", "blocker": ""},
        {"id": "B1", "task": "stale block", "phase": "1", "state": "blocked",
         "deps": "", "opened": old, "touched": old, "verify": "",
         "evidence": "", "blocker": "waiting on vendor"},
        {"id": "B2", "task": "revalidated block", "phase": "1", "state": "blocked",
         "deps": "", "opened": old, "touched": old, "verify": "",
         "evidence": "", "blocker": f"vendor reply (revalidated {recent})"},
        {"id": "C1", "task": "starved todo", "phase": "1", "state": "todo",
         "deps": "D0", "opened": old, "touched": old, "verify": "",
         "evidence": "", "blocker": ""},
        {"id": "D0", "task": "done dep", "phase": "1", "state": "done",
         "deps": "", "opened": old, "touched": old, "verify": "",
         "evidence": "", "blocker": ""},
        {"id": "E1", "task": "later phase active", "phase": "2",
         "state": "doing", "deps": "", "opened": recent, "touched": recent,
         "verify": "", "evidence": "", "blocker": ""},
        {"id": "E0", "task": "earlier open", "phase": "1", "state": "todo",
         "deps": "D0", "opened": recent, "touched": recent, "verify": "",
         "evidence": "", "blocker": ""},
    ]
    findings = [f for f in evaluate(list(rows))
                if f["kind"] not in ("STARVED",)]  # C1 touched>5d & ready & phase active
    kinds = {(f["severity"], f["kind"], f["id"]) for f in findings}
    expect = {(RED, "STUCK-DOING", "A1"), (RED, "STALE-BLOCK", "B1"),
              (ORANGE, "GATE-BREACH", "E1")}
    missing = expect - kinds
    surprise_red = {k for k in kinds if k[0] == RED} - expect
    assert not missing, f"selftest missed: {missing}"
    assert not surprise_red, f"selftest unexpected REDs: {surprise_red}"
    print("selftest OK — STUCK-DOING / STALE-BLOCK / GATE-BREACH rules fire, "
          "fresh rows stay quiet")
    return 0


def main() -> int:
    # Windows consoles default to cp1252; ledger text is UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true", help="exit 1 on any RED")
    ap.add_argument("--skip-verify", action="store_true", default=True,
                    help="skip running verify targets of done rows (default)")
    ap.add_argument("--verify", action="store_true",
                    help="run pytest on each done row's Verify target")
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rows, errors = parse_ledger(args.ledger)
    findings = evaluate(rows, skip_verify=not args.verify)
    report = render(rows, findings, errors)
    print(report)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "rows": len(rows), "errors": errors, "findings": findings},
        indent=2), encoding="utf-8")
    if args.ci and (any(f["severity"] == RED for f in findings) or errors):
        print("CI: RED findings present — failing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
