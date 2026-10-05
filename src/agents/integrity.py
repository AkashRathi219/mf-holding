"""Integrity auditor for displayed scheme-months (SPEC §11.1 / T20 / AC-13..15, AC-21).

Audits WHAT THE FRONTEND DISPLAYS: every row of the webapp ``schemes`` table
(the exact data the UI reads) together with its stored ``holdings`` rows,
opened through a READ-ONLY SQLite URI (``file:...?mode=ro``) — the auditor
never writes the DB and never mutates parsed JSON.  Each scheme is placed in
exactly one §11.0 tier by :func:`src.agents.tiers.classify` (the ladder is NOT
re-implemented here), keyed by scheme-month ``(amc, fund_name, as_of[:7])``.

Scheme-month key: ``schemes.as_of`` is a clean ``YYYY-MM-DD`` date for most
rows and ``as_of[:7]`` is the month bucket.  Rows with an EMPTY (or
non-date) ``as_of`` — 1053 of 3564 today — are audited under the documented
deterministic fallback bucket ``"unknown"`` and counted separately as
``unkeyable_schemes``: never dropped, never a crash.  Their sidecar key is
``"<amc>|<fund_name>|unknown"`` and a T2 among them is enqueued with
``month="unknown"`` so the gap stays visible to the channel owners.

``document_class`` recovery: the parsed stores under
``data/parsed/{amc_websites,advisorkhoj,amfi}`` are indexed ONCE at start
(~8.7k files — a per-scheme glob would be unusably slow) mapping
``(amc, fund_name, month)`` to the :func:`src.document_class.classify`
verdict of the parsed document, preferring the entry whose store matches the
DB row's ``source`` (``schemes.source`` is the source of the displayed
snapshot, so a document from a DIFFERENT store is not evidence about the
displayed class and the lookup fails to ``unknown``).  Parsed JSON predating
the ``document_class`` stamp carries no stored field — ``unknown`` is the
strict full-disclosure rule by design and can never silently become T1.

Actions (§11.1): a scheme-month whose ``TierResult.escalate`` is True is
enqueued onto the escalation queue — the queue itself additionally rejects
non-T2 tiers (defence in depth, AC-15), so a T1 ``TOP10_FALLBACK`` and a
suspect-scale flag sitting on a T0/T1 label never stack a ticket; the report
counts both layers.  The auditor never parks tickets and never edits
holdings — it only signals and reports.

Outputs (all paths overridable): ``data/reports/integrity_<date>.json`` and
``.md`` (tier histogram, SUSPECT_SCALE/MISSING/unkeyable/universe_only
counts, per-AMC breakdown, worst offenders) and the AC-21 sidecar
``data/reference/integrity_tiers.json`` keyed ``"<amc>|<fund_name>|<YYYY-MM>"``
for every audited scheme — the UI tier lookup (T36) reads it at query time;
no webapp DB schema change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from src.agents.escalation import (
    DEFAULT_MANUAL_CSV_PATH,
    DEFAULT_QUEUE_PATH,
    EscalationQueue,
)
from src.agents.tiers import (
    FLAG_MISSING,
    FLAG_SUSPECT_SCALE,
    TIER_CODES,
    TierResult,
    classify,
)
from src.document_class import UNKNOWN, classify as classify_document

DEFAULT_DB_PATH = Path("data/webapp.db")
DEFAULT_PARSED_ROOT = Path("data/parsed")
DEFAULT_REPORT_DIR = Path("data/reports")
DEFAULT_TIERS_OUT = Path("data/reference/integrity_tiers.json")

UNKNOWN_MONTH = "unknown"
TIER_ORDER: tuple[str, ...] = ("T0", "T1", "T2", "T3")
WORST_PER_AMC = 5
WORST_GLOBAL_N = 15
SUSPECT_TOP_N = 20

SOURCE_UNIVERSE_ONLY = "universe_only"

_WS_RE = re.compile(r"\s+")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
_SCHEME_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_RE_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_RE_DAY_MON_YEAR = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?[\s\-.]+([a-z]{3,})[\s\-.]+(\d{4})", re.IGNORECASE)
_RE_MON_DAY_YEAR = re.compile(
    r"([a-z]{3,})[\s\-.]+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})", re.IGNORECASE)
_RE_DOT_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_RE_MON_YEAR = re.compile(r"([a-z]{3,})[\s\-.]+(\d{4})", re.IGNORECASE)
_RE_AK_PATH_MONTH = re.compile(r"(?:^|[\\/])(\d{2})-(\d{4})(?:[\\/]|$)")


def _norm(name: object) -> str:
    """Join-key normaliser (mirrors webapp.db.norm_name; local to stay light)."""
    if not name:
        return ""
    return _NONALNUM_RE.sub("", _WS_RE.sub(" ", str(name)).lower())


def _month_from_date(text: object) -> str:
    """Extract a ``YYYY-MM`` bucket from a parsed document's date text ('' if none).

    Handles the verbose formats found in the parsed stores ('30 Jun 2026',
    'July 31, 2026', 'July 31st 2026', '31-JUL-2026', '15.06.2026',
    '2026-07-31', bare 'July 2026').  Only used on PARSED-SOURCE date fields;
    the DB ``schemes.as_of`` is already a clean date and is bucketed by
    ``as_of[:7]`` directly.
    """
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    m = _RE_ISO_DATE.search(s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    for m in _RE_DAY_MON_YEAR.finditer(s):
        mo = _MONTHS.get(m.group(2)[:3].lower())
        y = int(m.group(3))
        if mo and 2000 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"
    for m in _RE_MON_DAY_YEAR.finditer(s):
        mo = _MONTHS.get(m.group(1)[:3].lower())
        y = int(m.group(3))
        if mo and 2000 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"
    m = _RE_DOT_DATE.search(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}"
    for m in _RE_MON_YEAR.finditer(s):
        mo = _MONTHS.get(m.group(1)[:3].lower())
        y = int(m.group(2))
        if mo and 2000 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"
    return ""


def _month_from_path_parts(parts: tuple[str, ...] | list[str]) -> str:
    """``<AMC>/<YYYY>/<MM>/file.json`` folder month ('' when not that shape)."""
    if len(parts) >= 3 and len(parts[-3]) == 4 and parts[-3].isdigit() \
            and len(parts[-2]) == 2 and parts[-2].isdigit():
        y, mo = int(parts[-3]), int(parts[-2])
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    return ""


def _month_from_ak_path(inner: str) -> str:
    """Advisorkhoj inner document path month ('…\\07-2026\\portfolio\\…')."""
    m = _RE_AK_PATH_MONTH.search(inner or "")
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    return ""


def _scheme_month(as_of: object) -> tuple[str, bool]:
    """Month bucket + keyable flag from the DB ``schemes.as_of`` date string."""
    s = str(as_of or "").strip()
    if _SCHEME_MONTH_RE.match(s[:7]):
        return s[:7], True
    return UNKNOWN_MONTH, False


def _load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


@dataclass(frozen=True)
class ParsedEntry:
    """One parsed document's evidence: store source, class, relative path."""

    source: str
    document_class: str
    path: str


@dataclass
class ParsedIndex:
    """One-time index of the parsed stores for ``(amc, fund_name, month)`` joins.

    Built once per audit run (the stores hold ~8.7k files; a per-scheme glob
    would be unusably slow).  Only documents that the merge could actually
    have consumed are indexed — ``amc_websites`` files mirror the merge's skip
    rules (``report_*`` files, factsheet-named files, ``grouped_factsheet``
    metadata, empty ``schemes`` dicts, scheme payloads without holdings).
    Entries are registered under the month of the payload's own date (the
    field ``schemes.as_of`` is derived from); the folder/path month is used
    only when the payload date is absent or unparseable.
    """

    root: Path
    entries: dict[tuple[str, str, str], list[ParsedEntry]] = field(default_factory=dict)
    files_scanned: int = 0
    files_indexed: int = 0
    entries_without_month: int = 0

    @classmethod
    def build(cls, parsed_root: str | Path = DEFAULT_PARSED_ROOT) -> "ParsedIndex":
        index = cls(Path(parsed_root))
        index._index_amc_websites()
        index._index_advisorkhoj()
        index._index_amfi()
        for bucket in index.entries.values():
            bucket.sort(key=lambda e: e.path)
        return index

    def lookup(self, amc: str, fund_name: str, month: str, source: str) -> str:
        """Document class for the scheme-month, preferring the DB row's source.

        No entry, or no entry from the displayed snapshot's own store, means
        ``unknown`` — the strict full-disclosure rule (never silently T1).
        """
        bucket = self.entries.get((_norm(amc), _norm(fund_name), month))
        if not bucket:
            return UNKNOWN
        for entry in bucket:
            if entry.source == source:
                return entry.document_class
        return UNKNOWN

    def _register(self, amc: str, fund: str, month: str, source: str,
                  document_class: str, path: str) -> None:
        key = (_norm(amc), _norm(fund), month)
        if not key[0] or not key[1]:
            return
        if not month:
            self.entries_without_month += 1
            return
        entry = ParsedEntry(source=source, document_class=document_class, path=path)
        bucket = self.entries.setdefault(key, [])
        if entry not in bucket:
            bucket.append(entry)

    def _index_amc_websites(self) -> None:
        root = self.root / "amc_websites"
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*.json")):
            self.files_scanned += 1
            if path.name.startswith("report_") or "factsheet" in path.name.lower():
                continue
            doc = _load_json(path)
            if doc is None:
                continue
            meta = doc.get("metadata")
            if isinstance(meta, dict) and meta.get("grouped_factsheet"):
                continue
            schemes = doc.get("schemes")
            if not isinstance(schemes, dict) or not schemes:
                continue
            rel = path.relative_to(self.root).as_posix()
            document_class = classify_document(doc, source_file=rel)
            parts = path.relative_to(root).parts
            fallback_amc = parts[0].replace("_", " ") if parts else ""
            amc = str(doc.get("amc_name") or fallback_amc).strip()
            path_month = _month_from_path_parts(parts)
            indexed = False
            for payload in schemes.values():
                if not isinstance(payload, dict):
                    continue
                fund = payload.get("fund_name") or payload.get("scheme_name") or ""
                holdings = payload.get("holdings")
                if not isinstance(holdings, list) or not holdings:
                    continue
                month = _month_from_date(payload.get("date")) or path_month
                self._register(amc, str(fund), month, "amc_website", document_class, rel)
                indexed = True
            if indexed:
                self.files_indexed += 1

    def _index_advisorkhoj(self) -> None:
        root = self.root / "advisorkhoj"
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.json")):
            self.files_scanned += 1
            doc = _load_json(path)
            if doc is None:
                continue
            rel = path.relative_to(self.root).as_posix()
            amc = str(doc.get("amc") or path.stem).strip()
            indexed = False
            for f in doc.get("files") or []:
                if not isinstance(f, dict):
                    continue
                inner = str(f.get("file") or "")
                document_class = classify_document(doc, source_file=inner or rel)
                file_month = _month_from_ak_path(inner)
                for sheet in f.get("sheets") or []:
                    if not isinstance(sheet, dict):
                        continue
                    scheme = sheet.get("scheme")
                    if not scheme:
                        continue
                    plans = sheet.get("plans") or {}
                    if not any(
                        isinstance(p, dict) and p.get("holdings") for p in plans.values()
                    ):
                        continue
                    month = _month_from_date(sheet.get("date")) or file_month
                    self._register(amc, str(scheme), month, "advisorkhoj",
                                   document_class, rel)
                    indexed = True
            if indexed:
                self.files_indexed += 1

    def _index_amfi(self) -> None:
        root = self.root / "amfi"
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.json")):
            self.files_scanned += 1
            doc = _load_json(path)
            if doc is None:
                continue
            rel = path.relative_to(self.root).as_posix()
            amc = str(doc.get("amc") or path.stem).strip()
            document_class = classify_document(doc, source_file=rel)
            month = _month_from_date(doc.get("as_of"))
            indexed = False
            for fund, payload in (doc.get("schemes") or {}).items():
                if not isinstance(payload, dict) or not payload.get("holdings"):
                    continue
                self._register(amc, str(fund), month, "amfi", document_class, rel)
                indexed = True
            if indexed:
                self.files_indexed += 1


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open the webapp DB strictly read-only (URI ``?mode=ro``); never write."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"webapp DB not found: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


@dataclass
class SchemeAudit:
    """One audited scheme row: identity, month bucket and its TierResult."""

    scheme_id: int
    amc: str
    fund_name: str
    source: str
    as_of: str
    month: str
    keyable: bool
    result: TierResult


def _audit_db(
    db_path: str | Path,
    index: ParsedIndex,
    limit_amc: str | None = None,
) -> tuple[list[SchemeAudit], int, int]:
    """Read every scheme + its holding rows (read-only) and tier each one.

    Returns ``(audits, unkeyable_schemes, holding_rows_read)``.  Holdings are
    fetched in one pass and grouped by ``scheme_id``; ``percent_nav`` NULLs
    are kept (tiers.classify skips them in the coverage sum but still counts
    the row).  ``holdings.as_of`` is a free-text sentence and is deliberately
    NOT parsed — the scheme-month comes from ``schemes.as_of`` only.
    """
    con = _connect_ro(db_path)
    try:
        schemes = con.execute(
            "SELECT id, amc, fund_name, source, as_of FROM schemes ORDER BY id"
        ).fetchall()
        holdings: dict[int, list[object]] = {}
        holding_rows = 0
        for sid, pct in con.execute(
            "SELECT scheme_id, percent_nav FROM holdings ORDER BY scheme_id"
        ):
            holdings.setdefault(sid, []).append(pct)
            holding_rows += 1
    finally:
        con.close()
    limit = str(limit_amc).strip().lower() if limit_amc else None
    audits: list[SchemeAudit] = []
    unkeyable = 0
    for sid, amc, fund, source, as_of in schemes:
        amc, fund, source, as_of = str(amc or ""), str(fund or ""), str(source or ""), str(as_of or "")
        if limit is not None and amc.strip().lower() != limit:
            continue
        month, keyable = _scheme_month(as_of)
        if not keyable:
            unkeyable += 1
        rows = [{"percent_nav": v} for v in holdings.get(sid) or []]
        document_class = index.lookup(amc, fund, month, source)
        result = classify(rows, document_class)
        audits.append(SchemeAudit(
            scheme_id=sid, amc=amc, fund_name=fund, source=source, as_of=as_of,
            month=month, keyable=keyable, result=result,
        ))
    return audits, unkeyable, holding_rows


def _worst_entry(a: SchemeAudit) -> dict:
    return {
        "fund_name": a.fund_name,
        "month": a.month,
        "coverage_pct": a.result.coverage_pct,
        "n_holdings": a.result.n_holdings,
        "tier": a.result.tier,
        "tier_code": a.result.tier_code,
        "document_class": a.result.document_class,
        "flags": list(a.result.flags),
        "source": a.source,
    }


def _aggregate(
    audits: list[SchemeAudit],
    unkeyable: int,
    holding_rows: int,
) -> dict:
    tier_histogram = {code: 0 for code in TIER_ORDER}
    flags: Counter[str] = Counter()
    document_classes: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    per_amc: dict[str, dict] = {}
    universe_only = 0
    escalate_true = 0
    for a in audits:
        tier_histogram[a.result.tier_code] += 1
        flags.update(a.result.flags)
        document_classes[a.result.document_class] += 1
        sources[a.source] += 1
        if a.result.escalate:
            escalate_true += 1
        if a.source == SOURCE_UNIVERSE_ONLY:
            universe_only += 1
        slot = per_amc.setdefault(a.amc, {
            "n_schemes": 0,
            "tier_histogram": {code: 0 for code in TIER_ORDER},
            "unkeyable_schemes": 0,
            "universe_only_schemes": 0,
            "worst_offenders": [],
        })
        slot["n_schemes"] += 1
        slot["tier_histogram"][a.result.tier_code] += 1
        if not a.keyable:
            slot["unkeyable_schemes"] += 1
        if a.source == SOURCE_UNIVERSE_ONLY:
            slot["universe_only_schemes"] += 1
        slot["worst_offenders"].append(_worst_entry(a))
    for slot in per_amc.values():
        slot["worst_offenders"] = sorted(
            slot["worst_offenders"],
            key=lambda e: (e["coverage_pct"], e["fund_name"], e["month"]),
        )[:WORST_PER_AMC]
    worst_global = sorted(
        (_worst_entry(a) | {"amc": a.amc} for a in audits),
        key=lambda e: (e["coverage_pct"], e["amc"], e["fund_name"], e["month"]),
    )[:WORST_GLOBAL_N]
    suspects = sorted(
        (
            {
                "amc": a.amc,
                "fund_name": a.fund_name,
                "month": a.month,
                "coverage_pct": a.result.coverage_pct,
                "n_holdings": a.result.n_holdings,
                "tier": a.result.tier,
                "tier_code": a.result.tier_code,
                "document_class": a.result.document_class,
                "source": a.source,
            }
            for a in audits
            if FLAG_SUSPECT_SCALE in a.result.flags
        ),
        key=lambda e: (-e["coverage_pct"], e["amc"], e["fund_name"]),
    )[:SUSPECT_TOP_N]
    return {
        "n_schemes": len(audits),
        "n_schemes_with_holdings": sum(1 for a in audits if a.result.n_holdings > 0),
        "n_holding_rows": holding_rows,
        "unkeyable_schemes": unkeyable,
        "universe_only_schemes": universe_only,
        "tier_histogram": tier_histogram,
        "tier_names": {code: name for name, code in TIER_CODES.items()},
        "flags": dict(sorted(flags.items())),
        "document_classes": dict(sorted(document_classes.items())),
        "sources": dict(sorted(sources.items())),
        "escalate_true_schemes": escalate_true,
        "per_amc": dict(sorted(per_amc.items())),
        "worst_offenders_global": worst_global,
        "suspect_scale_top": suspects,
    }


def _sidecar_entries(audits: list[SchemeAudit]) -> dict[str, dict]:
    sidecar: dict[str, dict] = {}
    for a in audits:
        sidecar[f"{a.amc}|{a.fund_name}|{a.month}"] = {
            "tier": a.result.tier,
            "tier_code": a.result.tier_code,
            "document_class": a.result.document_class,
            "coverage_pct": a.result.coverage_pct,
            "n_holdings": a.result.n_holdings,
            "escalate": a.result.escalate,
            "flags": list(a.result.flags),
            "reason": a.result.reason,
            "source": a.source,
            "as_of": a.as_of,
        }
    return sidecar


def _enqueue_t2(
    audits: list[SchemeAudit],
    queue_path: str | Path,
    manual_csv_path: str | Path,
) -> dict:
    """Enqueue every escalate=True scheme-month; the queue rejects non-T2.

    Returns counters: ``new_tickets`` (appended this run), ``blocked_non_t2``
    (escalate=True but the queue's T2-only gate refused — e.g. a suspect-scale
    flag sitting on a T0/T1 label), ``skipped_blank_key`` (unusable identity).
    """
    queue = EscalationQueue(queue_path=queue_path, manual_csv_path=manual_csv_path)
    before = set(queue.load().keys())
    blocked_non_t2 = 0
    skipped_blank_key = 0
    for a in audits:
        if not a.result.escalate:
            continue
        if not a.amc.strip() or not a.fund_name.strip():
            skipped_blank_key += 1
            continue
        ticket = queue.enqueue(
            a.amc, a.fund_name, a.month, a.result.tier_code,
            a.result.coverage_pct, a.result.document_class,
        )
        if ticket is None:
            blocked_non_t2 += 1
    after = set(queue.load().keys())
    return {
        "new_tickets": len(after - before),
        "blocked_non_t2": blocked_non_t2,
        "skipped_blank_key": skipped_blank_key,
        "queue_path": str(queue_path),
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def run_audit(
    db_path: str | Path = DEFAULT_DB_PATH,
    parsed_root: str | Path = DEFAULT_PARSED_ROOT,
    *,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    tiers_out: str | Path = DEFAULT_TIERS_OUT,
    queue_path: str | Path = DEFAULT_QUEUE_PATH,
    manual_csv_path: str | Path = DEFAULT_MANUAL_CSV_PATH,
    enqueue: bool = True,
    limit_amc: str | None = None,
    index: ParsedIndex | None = None,
    audit_date: date | None = None,
) -> dict:
    """Run one full integrity audit pass and write reports + tier sidecar.

    Returns the report payload (also written to
    ``<report_dir>/integrity_<date>.json`` with an ``.md`` sibling).  When
    ``limit_amc`` is set only that AMC's schemes are audited and the sidecar
    is MERGED: entries for the scoped AMC are replaced, every other AMC's
    existing entries are preserved.
    """
    audit_day = audit_date or date.today()
    audit_date_str = audit_day.isoformat() if isinstance(audit_day, date) else str(audit_day)
    parsed_index = index if index is not None else ParsedIndex.build(parsed_root)
    audits, unkeyable, holding_rows = _audit_db(db_path, parsed_index, limit_amc=limit_amc)

    escalations = (
        _enqueue_t2(audits, queue_path, manual_csv_path)
        if enqueue
        else {
            "new_tickets": 0,
            "blocked_non_t2": 0,
            "skipped_blank_key": 0,
            "queue_path": str(queue_path),
        }
    )

    report = {
        "schema": "integrity_audit/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "audit_date": audit_date_str,
        "db_path": str(db_path),
        "parsed_root": str(parsed_root),
        "scope_amc": limit_amc,
        "enqueue": enqueue,
        **_aggregate(audits, unkeyable, holding_rows),
        "escalations_enqueued": escalations["new_tickets"],
        "escalations_blocked_non_t2": escalations["blocked_non_t2"],
        "escalations_skipped_blank_key": escalations["skipped_blank_key"],
        "escalation_queue_path": escalations["queue_path"],
        "tiers_out": str(tiers_out),
        "parsed_index": {
            "files_scanned": parsed_index.files_scanned,
            "files_indexed": parsed_index.files_indexed,
            "entries": len(parsed_index.entries),
            "entries_without_month": parsed_index.entries_without_month,
        },
    }

    sidecar = _sidecar_entries(audits)
    tiers_path = Path(tiers_out)
    if limit_amc:
        scoped_amcs = {a.amc for a in audits}
        existing = _load_json(tiers_path) or {}
        existing = {
            k: v for k, v in existing.items()
            if k.split("|", 1)[0] not in scoped_amcs
        }
        existing.update(sidecar)
        sidecar = existing
    _atomic_write_json(tiers_path, sidecar)
    report["tiers_entries"] = len(sidecar)

    report_dir_path = Path(report_dir)
    _atomic_write_json(report_dir_path / f"integrity_{audit_date_str}.json", report)
    _atomic_write_text(
        report_dir_path / f"integrity_{audit_date_str}.md", _render_markdown(report))
    return report


def _render_markdown(report: dict) -> str:
    hist = report["tier_histogram"]
    names = report["tier_names"]
    lines = [
        f"# Integrity audit — {report['audit_date']}",
        "",
        f"- DB: `{report['db_path']}` (read-only)",
        f"- Parsed stores: `{report['parsed_root']}`",
        f"- Scope: {report['scope_amc'] or 'all schemes'}",
        f"- Schemes audited: {report['n_schemes']} "
        f"(with holdings: {report['n_schemes_with_holdings']}); "
        f"holding rows: {report['n_holding_rows']}",
        "",
        "## Tier histogram (SPEC §11.0)",
        "",
        "| Tier | Name | Schemes |",
        "|---|---|---:|",
    ]
    for code in TIER_ORDER:
        lines.append(f"| {code} | {names.get(code, '')} | {hist.get(code, 0)} |")
    lines += [
        "",
        "## Counts",
        "",
        f"- SUSPECT_SCALE: {report['flags'].get(FLAG_SUSPECT_SCALE, 0)}",
        f"- MISSING: {report['flags'].get(FLAG_MISSING, 0)}",
        f"- unkeyable_schemes (empty/non-date as_of): {report['unkeyable_schemes']}",
        f"- universe_only schemes: {report['universe_only_schemes']}",
        f"- Escalate-flagged: {report['escalate_true_schemes']}; "
        f"new escalation tickets: {report['escalations_enqueued']}; "
        f"blocked non-T2: {report['escalations_blocked_non_t2']}",
        "",
        "## Document classes",
        "",
        "| Class | Schemes |",
        "|---|---:|",
    ]
    for dc, n in report["document_classes"].items():
        lines.append(f"| {dc} | {n} |")
    lines += [
        "",
        "## Per-AMC breakdown",
        "",
        "| AMC | Schemes | T0 | T1 | T2 | T3 | Unkeyable | Worst cov % |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for amc, slot in report["per_amc"].items():
        worst = (f"{slot['worst_offenders'][0]['coverage_pct']:.2f}"
                 if slot["worst_offenders"] else "")
        h = slot["tier_histogram"]
        lines.append(
            f"| {amc} | {slot['n_schemes']} | {h['T0']} | {h['T1']} | {h['T2']} | "
            f"{h['T3']} | {slot['unkeyable_schemes']} | {worst} |"
        )
    lines += ["", "## Worst offenders per AMC (lowest coverage, top "
              f"{WORST_PER_AMC})", ""]
    for amc, slot in report["per_amc"].items():
        lines.append(f"### {amc}")
        lines.append("")
        for e in slot["worst_offenders"]:
            flags = f" [{', '.join(e['flags'])}]" if e["flags"] else ""
            lines.append(
                f"- {e['coverage_pct']:.2f}% — {e['fund_name']} "
                f"({e['month']}, {e['tier_code']}, {e['document_class']}, "
                f"{e['source']}){flags}"
            )
        lines.append("")
    lines += [f"## Global worst offenders (top {WORST_GLOBAL_N})", ""]
    for e in report["worst_offenders_global"]:
        flags = f" [{', '.join(e['flags'])}]" if e["flags"] else ""
        lines.append(
            f"- {e['coverage_pct']:.2f}% — {e['fund_name']} ({e['amc']}, "
            f"{e['month']}, {e['tier_code']}, {e['document_class']}, "
            f"{e['source']}){flags}"
        )
    lines += ["", f"## Suspect-scale findings (top {SUSPECT_TOP_N} by coverage)", ""]
    if report["suspect_scale_top"]:
        for e in report["suspect_scale_top"]:
            lines.append(
                f"- {e['coverage_pct']:.2f}% — {e['fund_name']} ({e['amc']}, "
                f"{e['month']}, {e['tier_code']}, {e['document_class']}, "
                f"{e['source']}, {e['n_holdings']} rows)"
            )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _print_stdout(report: dict) -> None:
    hist = report["tier_histogram"]
    names = report["tier_names"]
    print(f"[integrity] DB {report['db_path']} (read-only): "
          f"{report['n_schemes']} schemes, {report['n_holding_rows']} holding rows")
    if report["scope_amc"]:
        print(f"[integrity] scope: AMC '{report['scope_amc']}'")
    pi = report["parsed_index"]
    print(f"[integrity] parsed index: {pi['files_scanned']} files scanned, "
          f"{pi['files_indexed']} indexed, {pi['entries']} entries "
          f"({pi['entries_without_month']} without month)")
    print("[integrity] tier histogram: " + ", ".join(
        f"{code} {names.get(code, '')}={hist.get(code, 0)}" for code in TIER_ORDER))
    print(f"[integrity] flags: SUSPECT_SCALE={report['flags'].get(FLAG_SUSPECT_SCALE, 0)}, "
          f"MISSING={report['flags'].get(FLAG_MISSING, 0)}")
    print(f"[integrity] unkeyable_schemes (empty as_of): {report['unkeyable_schemes']} | "
          f"universe_only: {report['universe_only_schemes']}")
    print(f"[integrity] escalations: {report['escalations_enqueued']} new tickets "
          f"({report['escalations_blocked_non_t2']} blocked non-T2) -> "
          f"{report['escalation_queue_path']}")
    print(f"[integrity] per-AMC ({len(report['per_amc'])}):")
    for amc, slot in report["per_amc"].items():
        h = slot["tier_histogram"]
        worst = (f"{slot['worst_offenders'][0]['coverage_pct']:.2f}%"
                 if slot["worst_offenders"] else "n/a")
        print(f"[integrity]   {amc}: {slot['n_schemes']} schemes | "
              f"T0={h['T0']} T1={h['T1']} T2={h['T2']} T3={h['T3']} | worst {worst}")
    print(f"[integrity] worst offenders (lowest coverage, top {WORST_GLOBAL_N}):")
    for e in report["worst_offenders_global"]:
        flags = f" [{', '.join(e['flags'])}]" if e["flags"] else ""
        print(f"[integrity]   {e['coverage_pct']:.2f}% - {e['fund_name']} "
              f"({e['amc']}, {e['month']}, {e['tier_code']}, "
              f"{e['document_class']}, {e['source']}){flags}")
    print(f"[integrity] suspect-scale top {SUSPECT_TOP_N}:")
    if report["suspect_scale_top"]:
        for e in report["suspect_scale_top"]:
            print(f"[integrity]   {e['coverage_pct']:.2f}% - {e['fund_name']} "
                  f"({e['amc']}, {e['month']}, {e['tier_code']}, "
                  f"{e['document_class']}, {e['source']}, {e['n_holdings']} rows)")
    else:
        print("[integrity]   none")
    print(f"[integrity] reports: integrity_{report['audit_date']}.json/.md written | "
          f"tier sidecar: {report['tiers_out']} "
          f"({report['tiers_entries']} entries)")
    print(f"[integrity] done: {report['n_schemes']} schemes audited, "
          f"{report['escalations_enqueued']} tickets enqueued")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.agents.integrity",
        description="Integrity auditor (SPEC §11.1 / T20): tier every displayed "
                    "scheme-month, write reports + tier sidecar, enqueue T2 escalations.",
    )
    parser.add_argument("--audit", action="store_true",
                        help="run the full audit pass")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH),
                        help="webapp SQLite DB (opened read-only)")
    parser.add_argument("--parsed-root", default=str(DEFAULT_PARSED_ROOT),
                        help="parsed stores root (amc_websites/advisorkhoj/amfi)")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--tiers-out", default=str(DEFAULT_TIERS_OUT))
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE_PATH),
                        help="escalation queue JSONL (append-only, deduped)")
    parser.add_argument("--no-enqueue", action="store_true",
                        help="audit only; never append escalation tickets")
    parser.add_argument("--limit-amc", default=None,
                        help="audit only this AMC (fast scoped run; sidecar merged)")
    args = parser.parse_args(argv)
    if not args.audit:
        parser.print_help()
        return 2
    report = run_audit(
        args.db,
        args.parsed_root,
        report_dir=args.report_dir,
        tiers_out=args.tiers_out,
        queue_path=args.queue,
        enqueue=not args.no_enqueue,
        limit_amc=args.limit_amc,
    )
    _print_stdout(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
