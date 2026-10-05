"""Append-only episode journal for the AMC holdings agents (SPEC §5 step 5/§7, T2).

Every agent run records one immutable episode - the §7 record
``{timestamp, episode_id, mf_id, amc_name, fingerprint, strategy_applied,
tools_used, outcome, failure_code, reward, discovered_count, downloaded_count,
evidence}`` plus the backward-compatible ``trigger``/``channel`` keys used by
escalation-driven runs (§11.3) - into
``data/logs/agent_episodes/<YYYY-MM>/<mf_id>_<safe_amc>.jsonl``.

The journal is append-only: :meth:`EpisodeJournal.append` opens the month file
in ``"a"`` mode and writes each record as ONE ``"\\n"``-terminated JSON line
followed by ``flush()``, so concurrent writers never interleave partial lines.
If the file's tail lost its newline (a crash mid-append) the stale fragment is
terminated first so one bad crash cannot poison the next record; history is
never rewritten or deleted.

Dedupe (PLAN §4.1): :func:`load` folds the lines in order keyed by
``episode_id`` with last write wins, so re-running the same episode never
double-counts; :func:`count` counts the distinct ids and :func:`iter_episodes`
streams the raw records, skipping unparseable or partial lines defensively.

Validation gate (AC-5, 0 unclassified errors): ``outcome`` must be one of
``SUCCESS | PARTIAL | FAILED`` and ``failure_code`` must be ``None`` on
SUCCESS and otherwise exactly one of the 15 stable codes in
``src.agents.taxonomy.FAILURE_CODES``; anything else raises ``ValueError``
before anything is written.

All paths are injectable (the journal root defaults to the SPEC location
relative to the ``mf_holding`` working directory) and timestamps are
injectable via the ``now`` argument, so tests and the scheduler can run fully
deterministic against a sandbox.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path

from src.agents.taxonomy import is_valid_code

DEFAULT_ROOT = Path("data/logs/agent_episodes")

OUTCOME_SUCCESS = "SUCCESS"
OUTCOME_PARTIAL = "PARTIAL"
OUTCOME_FAILED = "FAILED"
OUTCOMES: tuple[str, ...] = (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_FAILED)

TRIGGER_ESCALATION = "escalation"

FINGERPRINT_KEYS: tuple[str, ...] = ("cdn", "cms", "auth")
EVIDENCE_KEYS: tuple[str, ...] = ("sample_url", "elapsed_sec")

REQUIRED_KEYS: tuple[str, ...] = (
    "episode_id",
    "mf_id",
    "amc_name",
    "fingerprint",
    "strategy_applied",
    "tools_used",
    "outcome",
    "failure_code",
    "reward",
    "discovered_count",
    "downloaded_count",
    "evidence",
)

EPISODE_SCHEMA_KEYS: tuple[str, ...] = ("timestamp", *REQUIRED_KEYS, "trigger", "channel")

_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_amc(name: object) -> str:
    """Filename-safe AMC slug: spaces become ``_`` and ``/`` becomes ``-``."""
    return str(name).strip().replace(" ", "_").replace("/", "-")


def month_of(timestamp: object) -> str:
    """``YYYY-MM`` shard of an ISO-8601 timestamp; raises ``ValueError`` otherwise."""
    match = _MONTH_PATTERN.match(str(timestamp).strip())
    if match is None:
        raise ValueError(f"timestamp must be ISO-8601 with a YYYY-MM prefix, got {timestamp!r}")
    return match.group(0)


def new_episode_id(mf_id: object, strategy_applied: object, *, now: object = None) -> str:
    """``ep_<mf_id>_<UTC-epoch-ms>_<strategy-slug>`` (PLAN §4.1); ``now`` injectable."""
    moment = _coerce_datetime(now) if now is not None else datetime.now(timezone.utc)
    return f"ep_{mf_id}_{int(moment.timestamp() * 1000)}_{safe_amc(strategy_applied)}"


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_timestamp(episode_ts: object, now: object) -> str:
    if now is not None:
        return _coerce_timestamp(now)
    if episode_ts is not None:
        return str(episode_ts)
    return _now()


def _coerce_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_number(value: object, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {value!r}") from None


def _as_number_or_none(value: object, field: str) -> float | None:
    return None if value is None else _as_number(value, field)


def _as_int(value: object, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer, got {value!r}") from None


def _as_str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def normalize_episode(episode: Mapping[str, object], *, now: object = None) -> dict[str, object]:
    """Validate ``episode`` against the §7 schema and return the writable record.

    Every schema key is present on the returned record: ``timestamp`` is
    resolved from the explicit ``now`` argument, the episode's own value or
    the current time (in that order); ``trigger``/``channel`` default to
    ``None``; missing ``fingerprint``/``evidence`` sub-keys default to
    ``None``.  ``outcome`` and ``failure_code`` are validated per AC-5.
    Unknown extra keys are dropped so stored records stay schema-exact.
    """
    if not isinstance(episode, Mapping):
        raise ValueError("episode must be a mapping of episode schema keys")
    missing = [key for key in REQUIRED_KEYS if key not in episode]
    if missing:
        raise ValueError(f"episode is missing required schema keys: {', '.join(missing)}")

    episode_id = str(episode["episode_id"]).strip()
    if not episode_id:
        raise ValueError("episode_id must be a non-empty string")
    mf_id = str(episode["mf_id"]).strip()
    if not mf_id:
        raise ValueError("mf_id must be a non-empty string")

    outcome = episode["outcome"]
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")

    failure_code = episode["failure_code"]
    if failure_code is not None and not is_valid_code(failure_code):
        raise ValueError(f"failure_code {failure_code!r} is not one of the 15 taxonomy codes (AC-5)")
    if outcome == OUTCOME_SUCCESS and failure_code is not None:
        raise ValueError("a SUCCESS episode must carry failure_code=None")
    if outcome != OUTCOME_SUCCESS and failure_code is None:
        raise ValueError(f"a {outcome} episode must carry exactly one taxonomy failure_code, got None")

    fingerprint = episode["fingerprint"]
    if not isinstance(fingerprint, Mapping):
        raise ValueError("fingerprint must be a mapping with cdn/cms/auth keys")
    evidence = episode["evidence"]
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be a mapping with sample_url/elapsed_sec keys")
    tools_used = episode["tools_used"]
    if isinstance(tools_used, tuple):
        tools_used = list(tools_used)
    if not isinstance(tools_used, list):
        raise ValueError("tools_used must be a list of tool names")

    return {
        "timestamp": _resolve_timestamp(episode.get("timestamp"), now),
        "episode_id": episode_id,
        "mf_id": mf_id,
        "amc_name": str(episode["amc_name"]),
        "fingerprint": {key: _as_str_or_none(fingerprint.get(key)) for key in FINGERPRINT_KEYS},
        "strategy_applied": str(episode["strategy_applied"]),
        "tools_used": [str(tool) for tool in tools_used],
        "outcome": outcome,
        "failure_code": failure_code,
        "reward": _as_number(episode["reward"], "reward"),
        "discovered_count": _as_int(episode["discovered_count"], "discovered_count"),
        "downloaded_count": _as_int(episode["downloaded_count"], "downloaded_count"),
        "evidence": {
            "sample_url": _as_str_or_none(evidence.get("sample_url")),
            "elapsed_sec": _as_number_or_none(evidence.get("elapsed_sec"), "evidence.elapsed_sec"),
        },
        "trigger": _as_str_or_none(episode.get("trigger")),
        "channel": _as_str_or_none(episode.get("channel")),
    }


def iter_episodes(path: str | Path) -> Iterator[dict[str, object]]:
    """Stream the parsed records of one month file in order, skipping bad lines.

    Unparseable, partial or non-object lines (e.g. a crash mid-append) are
    skipped defensively without raising, mirroring the escalation queue fold.
    """
    file_path = Path(path)
    if not file_path.exists():
        return
    with open(file_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def load(path: str | Path) -> dict[str, dict[str, object]]:
    """Fold the file in order, last write wins per ``episode_id`` (PLAN §4.1)."""
    episodes: dict[str, dict[str, object]] = {}
    for record in iter_episodes(path):
        episode_id = record.get("episode_id")
        if not episode_id:
            continue
        episodes[str(episode_id)] = record
    return episodes


def count(path: str | Path) -> int:
    """Distinct episodes after folding - a re-run of one episode counts once."""
    return len(load(path))


class EpisodeJournal:
    """Append-only episode journal sharded ``<root>/<YYYY-MM>/<mf_id>_<safe_amc>.jsonl``."""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)

    def path_for(self, mf_id: object, amc_name: object, month: str) -> Path:
        """Month shard path for one AMC (SPEC §7 ``<mf_id>_<safe_name>.jsonl``)."""
        return self.root / str(month) / f"{mf_id}_{safe_amc(amc_name)}.jsonl"

    def append(self, episode: Mapping[str, object], *, now: object = None) -> Path:
        """Validate one episode and append it; returns the month file written to.

        The record is validated in full (AC-5 gate) before the file is opened,
        the ``<YYYY-MM>`` shard comes from the resolved timestamp and the
        parent directories are created on demand.
        """
        record = normalize_episode(episode, now=now)
        path = self.path_for(record["mf_id"], record["amc_name"], month_of(record["timestamp"]))
        _append_line(path, json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def iter_episodes(self, path: str | Path) -> Iterator[dict[str, object]]:
        return iter_episodes(path)

    def load(self, path: str | Path) -> dict[str, dict[str, object]]:
        return load(path)

    def count(self, path: str | Path) -> int:
        return count(path)


def _append_line(path: Path, line: str) -> None:
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


__all__ = [
    "DEFAULT_ROOT",
    "EPISODE_SCHEMA_KEYS",
    "EVIDENCE_KEYS",
    "FINGERPRINT_KEYS",
    "OUTCOMES",
    "OUTCOME_FAILED",
    "OUTCOME_PARTIAL",
    "OUTCOME_SUCCESS",
    "REQUIRED_KEYS",
    "TRIGGER_ESCALATION",
    "EpisodeJournal",
    "count",
    "iter_episodes",
    "load",
    "month_of",
    "new_episode_id",
    "normalize_episode",
    "safe_amc",
]
