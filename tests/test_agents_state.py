"""T5: agent state persistence - atomic per-AMC state files and a durable
per-host circuit breaker whose 30-minute timestamps survive a process
restart (SPEC §4 rate-limit politeness, §7 storage paths, §8.2 breaker, AC-6).

Every test writes into pytest's ``tmp_path`` sandbox; the real ``data/`` tree
is never touched (the root is overridable for exactly that reason).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.agents.state import (
    BACKOFF_SECONDS,
    DEFAULT_ROOT,
    TRIP_THRESHOLD,
    AgentState,
    empty_state,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 4, 6, 0, 0, tzinfo=UTC)
HOST = "www.axismf.com"


def test_round_trip_preserves_fields_via_fresh_instance(tmp_path):
    first = AgentState("11", root=tmp_path)
    first.record_block(HOST, code="429", now=T0)
    first.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015", now=T0 + timedelta(seconds=1))
    first.record_success("advisorkhoj.com", now=T0 + timedelta(seconds=2))

    raw = json.loads((tmp_path / "11.json").read_text(encoding="utf-8"))
    assert raw["version"] == "1.0.0"
    assert raw["hosts"][HOST]["consecutive_blocks"] == 2
    assert raw["hosts"][HOST]["last_block_code"] == "ERR_WAF_CLOUDFLARE_1015"
    assert raw["hosts"][HOST]["last_block_at"] == (T0 + timedelta(seconds=1)).isoformat()
    assert raw["hosts"]["advisorkhoj.com"]["last_success_at"] == (
        T0 + timedelta(seconds=2)
    ).isoformat()

    second = AgentState("11", root=tmp_path)
    assert second.snapshot() == first.snapshot()
    assert second.snapshot()["hosts"][HOST]["blocked_until"] == (
        T0 + timedelta(seconds=1) + timedelta(seconds=BACKOFF_SECONDS)
    ).isoformat()
    until = datetime.fromisoformat(second.snapshot()["hosts"][HOST]["blocked_until"])
    assert until.utcoffset() == timedelta(0)


def test_single_block_does_not_trip_breaker(tmp_path):
    assert TRIP_THRESHOLD == 2
    state = AgentState("11", root=tmp_path)
    state.record_block(HOST, code="429", now=T0)
    assert state.is_blocked(HOST, now=T0) is False
    assert state.remaining_backoff(HOST, now=T0) == 0.0
    entry = state.snapshot()["hosts"][HOST]
    assert entry["consecutive_blocks"] == 1
    assert entry["blocked_until"] is None


def test_second_consecutive_block_trips_breaker(tmp_path):
    state = AgentState("11", root=tmp_path)
    state.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015", now=T0)
    state.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015", now=T0 + timedelta(seconds=2))
    probe = T0 + timedelta(seconds=3)
    assert state.is_blocked(HOST, now=probe) is True
    assert state.remaining_backoff(HOST, now=probe) == pytest.approx(BACKOFF_SECONDS - 1.0)
    assert state.snapshot()["hosts"][HOST]["blocked_until"] == (
        T0 + timedelta(seconds=2) + timedelta(seconds=BACKOFF_SECONDS)
    ).isoformat()


def test_breaker_opens_when_30_minute_window_elapses(tmp_path):
    state = AgentState("11", root=tmp_path)
    state.record_block(HOST, code="429", now=T0)
    state.record_block(HOST, code="429", now=T0 + timedelta(seconds=1))
    until = T0 + timedelta(seconds=1) + timedelta(seconds=BACKOFF_SECONDS)
    assert state.is_blocked(HOST, now=until - timedelta(seconds=1)) is True
    assert state.is_blocked(HOST, now=until) is False
    assert state.is_blocked(HOST, now=until + timedelta(hours=1)) is False
    assert state.remaining_backoff(HOST, now=until + timedelta(hours=1)) == 0.0


def test_remaining_backoff_counts_down_to_zero(tmp_path):
    state = AgentState("11", root=tmp_path)
    state.record_block(HOST, code="429", now=T0)
    state.record_block(HOST, code="429", now=T0)
    assert state.remaining_backoff(HOST, now=T0) == pytest.approx(1800.0)
    assert state.remaining_backoff(HOST, now=T0 + timedelta(seconds=900)) == pytest.approx(900.0)
    assert state.remaining_backoff(HOST, now=T0 + timedelta(seconds=1799)) == pytest.approx(1.0)
    assert state.remaining_backoff(HOST, now=T0 + timedelta(seconds=1800)) == 0.0
    assert state.remaining_backoff(HOST, now=T0 + timedelta(seconds=7200)) == 0.0
    assert state.remaining_backoff("never-blocked.example.com", now=T0) == 0.0


def test_record_success_resets_counter_so_non_consecutive_blocks_do_not_trip(tmp_path):
    state = AgentState("11", root=tmp_path)
    state.record_block(HOST, code="429", now=T0)
    state.record_success(HOST, now=T0 + timedelta(seconds=30))
    state.record_block(HOST, code="429", now=T0 + timedelta(seconds=60))
    probe = T0 + timedelta(seconds=61)
    assert state.is_blocked(HOST, now=probe) is False
    assert state.remaining_backoff(HOST, now=probe) == 0.0
    entry = state.snapshot()["hosts"][HOST]
    assert entry["consecutive_blocks"] == 1
    assert entry["last_success_at"] == (T0 + timedelta(seconds=30)).isoformat()


def test_breaker_survives_restart_fresh_instance_reads_disk(tmp_path):
    dead_process = AgentState("53", root=tmp_path)
    dead_process.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015", now=T0)
    dead_process.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015", now=T0 + timedelta(seconds=1))

    restarted = AgentState("53", root=tmp_path)
    mid_window = T0 + timedelta(seconds=900)
    assert restarted.is_blocked(HOST, now=mid_window) is True
    assert restarted.remaining_backoff(HOST, now=mid_window) == pytest.approx(901.0)

    again = AgentState("53", root=tmp_path)
    assert again.is_blocked(HOST, now=T0 + timedelta(seconds=1799)) is True
    assert again.is_blocked(HOST, now=T0 + timedelta(seconds=1801)) is False

    raw = json.loads((tmp_path / "53.json").read_text(encoding="utf-8"))
    until = datetime.fromisoformat(raw["hosts"][HOST]["blocked_until"])
    assert until.utcoffset() == timedelta(0)
    assert (until - (T0 + timedelta(seconds=1))).total_seconds() == pytest.approx(BACKOFF_SECONDS)


def test_blocks_are_tracked_per_host(tmp_path):
    state = AgentState("11", root=tmp_path)
    state.record_block("blocked.example.com", code="429", now=T0)
    state.record_block("blocked.example.com", code="429", now=T0 + timedelta(seconds=1))
    probe = T0 + timedelta(seconds=2)
    assert state.is_blocked("blocked.example.com", now=probe) is True
    assert state.is_blocked("other.example.com", now=probe) is False
    assert state.remaining_backoff("other.example.com", now=probe) == 0.0

    state.record_block("other.example.com", code="503", now=probe)
    state.record_block("other.example.com", code="503", now=probe + timedelta(seconds=1))
    later = probe + timedelta(seconds=2)
    assert state.is_blocked("other.example.com", now=later) is True
    assert state.is_blocked("blocked.example.com", now=later) is True

    state.record_success("other.example.com", now=later + timedelta(seconds=1))
    hosts = state.snapshot()["hosts"]
    assert hosts["blocked.example.com"]["consecutive_blocks"] == 2
    assert hosts["other.example.com"]["consecutive_blocks"] == 0
    assert state.is_blocked("blocked.example.com", now=later + timedelta(seconds=1)) is True


def test_atomic_write_leaves_no_temp_files_behind(tmp_path):
    state = AgentState("11", root=tmp_path)
    for i in range(6):
        state.record_block(HOST, code="429", now=T0 + timedelta(seconds=i))
    state.record_success(HOST, now=T0 + timedelta(seconds=60))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["11.json"]
    raw = json.loads((tmp_path / "11.json").read_text(encoding="utf-8"))
    assert raw["hosts"][HOST]["consecutive_blocks"] == 0


def test_missing_state_file_starts_empty(tmp_path):
    state = AgentState("11", root=tmp_path)
    assert state.snapshot()["hosts"] == {}
    assert state.is_blocked(HOST, now=T0) is False
    assert state.remaining_backoff(HOST, now=T0) == 0.0


def test_corrupt_state_file_loads_as_empty_instead_of_raising(tmp_path):
    (tmp_path / "11.json").write_text("{not json", encoding="utf-8")
    state = AgentState("11", root=tmp_path)
    snap = state.snapshot()
    assert snap["hosts"] == {}
    assert snap["version"] == "1.0.0"
    assert state.is_blocked(HOST, now=T0) is False
    state.record_block(HOST, code="429", now=T0)
    state.record_block(HOST, code="429", now=T0 + timedelta(seconds=1))
    assert state.is_blocked(HOST, now=T0 + timedelta(seconds=2)) is True


def test_now_accepts_iso_strings_and_default_root_is_spec_path(tmp_path):
    assert DEFAULT_ROOT == Path("data/logs/agent_state")
    assert AgentState("11").path == DEFAULT_ROOT / "11.json"
    state = AgentState("11", root=tmp_path)
    state.record_block(HOST, code="429", now="2026-10-04T06:00:00+00:00")
    state.record_block(HOST, code="429", now="2026-10-04T06:00:01+00:00")
    assert state.is_blocked(HOST, now="2026-10-04T06:00:02+00:00") is True
    assert state.is_blocked(HOST, now="2026-10-04T06:30:01+00:00") is False
