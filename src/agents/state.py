"""Durable per-AMC agent state with a per-host circuit breaker (SPEC §4, §7, §8.2; AC-6).

Each AMC agent persists its own state to ``data/logs/agent_state/<mf_id>.json``
(SPEC §7). The file is the ONLY carrier of state: there is no module-level
cache - every query re-reads the file from disk and every mutation re-reads,
updates and atomically rewrites it - so the circuit-breaker timestamps survive
a process restart: a brand-new :class:`AgentState` instance reports a blocked
host for the full window (AC-6: after HTTP 1015 the agent dispatches zero
network calls to that host for at least 1800 seconds).

Circuit breaker (SPEC §4 rate-limit politeness, §8.2): block events are
tracked per HOST, never per AMC. Two consecutive block events for the same
host - HTTP 429, Cloudflare ``ERR_WAF_CLOUDFLARE_1015`` or any 5xx response,
as classified by the caller - open a 30-minute (1800 s) backoff lock for that
host by stamping an absolute ISO-8601 UTC ``blocked_until`` timestamp. A
successful interaction (:meth:`AgentState.record_success`) resets the
consecutive counter so only uninterrupted block runs trip the breaker; the
time lock itself never gets cut short by a success and only expires by
elapsing. Further blocks while the lock is open (or after it, with no success
in between) re-stamp ``blocked_until`` from the latest block - no hammering.

Persistence safety: writes are atomic. The payload goes to a uniquely named
temp file in the SAME directory, is flushed and fsynced, then moved into place
with ``os.replace``. A process dying mid-write can therefore never leave a
truncated or corrupt state file - readers see either the previous or the new
file, never a partial one - and the temp file is removed on any failure so no
litter is left behind. A missing, corrupt or wrong-shaped file loads as an
empty state instead of raising (mirroring ``knowledge.load``); the rate
limiter's politeness delays remain in force either way.

Imports stay deliberately light (json/os/datetime/pathlib only): no httpx, no
Playwright, no database modules. Time is injectable (``now=``) on every
method so tests and the scheduler run fully deterministic. Concurrent writers
to the same state file get last-write-wins on whole-file granularity - the
atomic replace prevents corruption, and per-AMC files keep contention to one
writer per agent in practice.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ROOT = Path("data/logs/agent_state")

STATE_VERSION = "1.0.0"
TRIP_THRESHOLD = 2
BACKOFF_SECONDS = 1800.0


def _clean_host(host: object) -> str:
    host_s = str(host).strip().lower()
    if not host_s:
        raise ValueError("host must be a non-empty string")
    return host_s


def _clean_mf_id(mf_id: object) -> str:
    mf_id_s = str(mf_id).strip()
    if not mf_id_s or mf_id_s in (".", "..") or "/" in mf_id_s or "\\" in mf_id_s:
        raise ValueError(f"mf_id must be a safe single file name, got {mf_id!r}")
    return mf_id_s


def _coerce_now(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        return _coerce_now(datetime.fromisoformat(now.replace("Z", "+00:00")))
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime, an ISO-8601 string or None")
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def empty_state(now: datetime | str | None = None) -> dict:
    """Return a fresh empty state document in the storage-contract shape."""
    return {"version": STATE_VERSION, "updated_at": _iso(_coerce_now(now)), "hosts": {}}


def _new_host_entry() -> dict:
    return {
        "consecutive_blocks": 0,
        "blocked_until": None,
        "last_block_code": None,
        "last_block_at": None,
        "last_success_at": None,
    }


def _normalize_entry(entry: dict) -> dict:
    blocks = entry.get("consecutive_blocks")
    if isinstance(blocks, bool) or not isinstance(blocks, int) or blocks < 0:
        blocks = 0
    normalized = _new_host_entry()
    normalized["consecutive_blocks"] = blocks
    for key in ("blocked_until", "last_block_code", "last_block_at", "last_success_at"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value
    return normalized


def _normalize_state(raw: dict) -> dict:
    version = raw.get("version")
    updated_at = raw.get("updated_at")
    state: dict = {
        "version": version if isinstance(version, str) and version.strip() else STATE_VERSION,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
        "hosts": {},
    }
    hosts_raw = raw.get("hosts")
    if isinstance(hosts_raw, dict):
        for key, entry in hosts_raw.items():
            if isinstance(key, str) and key.strip() and isinstance(entry, dict):
                state["hosts"][key] = _normalize_entry(entry)
    return state


def _atomic_write_text(text: str, path: Path) -> None:
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


class AgentState:
    """Durable per-AMC agent state; every read hits disk, every write is atomic."""

    def __init__(self, mf_id: object, *, root: str | Path = DEFAULT_ROOT) -> None:
        self.mf_id = _clean_mf_id(mf_id)
        self.root = Path(root)
        self.path = self.root / f"{self.mf_id}.json"

    def __repr__(self) -> str:
        return f"AgentState(mf_id={self.mf_id!r}, path={str(self.path)!r})"

    def _read(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_state()
        except (OSError, UnicodeDecodeError):
            return empty_state()
        try:
            raw = json.loads(text)
        except ValueError:
            return empty_state()
        if not isinstance(raw, dict):
            return empty_state()
        return _normalize_state(raw)

    def _write(self, state: dict) -> None:
        _atomic_write_text(
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", self.path
        )

    def snapshot(self) -> dict:
        """Return a copy of the full persisted state document."""
        return json.loads(json.dumps(self._read(), ensure_ascii=False))

    def hosts(self) -> tuple[str, ...]:
        """Sorted tuple of hosts currently tracked in the state file."""
        return tuple(sorted(self._read()["hosts"]))

    def record_block(
        self, host: str, *, code: str | None = None, now: datetime | str | None = None
    ) -> dict:
        """Record one blocking event for ``host`` and trip the breaker per §8.2.

        The caller classifies the event (HTTP 429, ``ERR_WAF_CLOUDFLARE_1015``
        or a 5xx response) and passes its code for evidence. The consecutive
        counter increments; from the threshold up, ``blocked_until`` is
        (re-)stamped to ``now + BACKOFF_SECONDS`` as an absolute ISO-8601 UTC
        timestamp. Returns a copy of the persisted host entry.
        """
        host_s = _clean_host(host)
        now_dt = _coerce_now(now)
        state = self._read()
        hosts = state["hosts"]
        entry = hosts.get(host_s)
        if not isinstance(entry, dict):
            entry = _new_host_entry()
            hosts[host_s] = entry
        entry["consecutive_blocks"] = int(entry.get("consecutive_blocks") or 0) + 1
        entry["last_block_code"] = None if code is None else str(code)
        entry["last_block_at"] = _iso(now_dt)
        if entry["consecutive_blocks"] >= TRIP_THRESHOLD:
            entry["blocked_until"] = _iso(now_dt + timedelta(seconds=BACKOFF_SECONDS))
        state["updated_at"] = _iso(now_dt)
        self._write(state)
        return dict(entry)

    def record_success(
        self, host: str, *, now: datetime | str | None = None
    ) -> dict:
        """Record a successful interaction; resets the consecutive counter.

        An open ``blocked_until`` lock is deliberately left in place and only
        expires by elapsing, so a stray success can never shorten a 1015
        backoff window (AC-6). Returns a copy of the persisted host entry.
        """
        host_s = _clean_host(host)
        now_dt = _coerce_now(now)
        state = self._read()
        hosts = state["hosts"]
        entry = hosts.get(host_s)
        if not isinstance(entry, dict):
            entry = _new_host_entry()
            hosts[host_s] = entry
        entry["consecutive_blocks"] = 0
        entry["last_success_at"] = _iso(now_dt)
        state["updated_at"] = _iso(now_dt)
        self._write(state)
        return dict(entry)

    def is_blocked(self, host: str, *, now: datetime | str | None = None) -> bool:
        """True while the host's 30-minute backoff window is open."""
        return self.remaining_backoff(host, now=now) > 0.0

    def remaining_backoff(self, host: str, *, now: datetime | str | None = None) -> float:
        """Seconds left on the host's backoff lock; ``0.0`` when clear."""
        host_s = _clean_host(host)
        now_dt = _coerce_now(now)
        entry = self._read()["hosts"].get(host_s)
        if not isinstance(entry, dict):
            return 0.0
        until = _parse_ts(entry.get("blocked_until"))
        if until is None:
            return 0.0
        return max(0.0, (until - now_dt).total_seconds())


__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_ROOT",
    "STATE_VERSION",
    "TRIP_THRESHOLD",
    "AgentState",
    "empty_state",
]
