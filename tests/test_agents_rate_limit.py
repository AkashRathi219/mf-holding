"""T6: per-host rate limiter + circuit-breaker facade (SPEC §4, §8.2; AC-6).

Every test drives the limiter with a fake injectable clock (an epoch-seconds
float) and, where waiting is exercised, an injected fake sleeper that advances
that same clock - the suite never pays for a real ``time.sleep``. An autouse
fixture replaces ``time.sleep`` with a bomb that fails any test accidentally
hitting the default sleeper, proving the politeness waits are fully
injectable. The durable breaker half delegates to ``src.agents.state`` and is
exercised against ``tmp_path`` state files only.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from src.agents import rate_limit
from src.agents.rate_limit import (
    BACKOFF_SECONDS,
    MAX_CONCURRENT_PER_HOST,
    MIN_SPACING_SECONDS,
    HostBlocked,
    HostBusy,
    RateLimiter,
    is_blocking,
)
from src.agents.state import BACKOFF_SECONDS as STATE_BACKOFF_SECONDS
from src.agents.state import AgentState

HOST = "www.axismf.com"
OTHER = "www.nipponindia.com"


class FakeClock:
    """Zero-arg callable returning a controllable epoch-seconds float."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = float(start)
        self.naps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)

    def sleeper(self):
        def _sleep(delay: float) -> None:
            self.naps.append(float(delay))
            self.advance(delay)

        return _sleep

    def async_sleeper(self):
        async def _sleep(delay: float) -> None:
            self.naps.append(float(delay))
            self.advance(delay)

        return _sleep


class _RealSleepBomb:
    """Stands in for ``time.sleep``; any call fails the owning test."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, delay: float = 0.0) -> None:
        self.calls.append(float(delay))
        raise AssertionError(f"real time.sleep({delay!r}) was called - inject a fake sleeper instead")


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> _RealSleepBomb:
    bomb = _RealSleepBomb()
    monkeypatch.setattr(time, "sleep", bomb)
    return bomb


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def agent_state(tmp_path: Path) -> AgentState:
    return AgentState("11", root=tmp_path)


def test_constants_are_spec_values_and_backoff_is_reexported():
    assert MIN_SPACING_SECONDS == 1.5
    assert MAX_CONCURRENT_PER_HOST == 2
    assert BACKOFF_SECONDS == pytest.approx(1800.0)
    assert rate_limit.BACKOFF_SECONDS is STATE_BACKOFF_SECONDS
    assert rate_limit.WAF_1015_CODE == "ERR_WAF_CLOUDFLARE_1015"


def test_same_host_requests_are_spaced_at_least_1_5s_apart(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    first = lim.acquire(HOST)
    assert first.started_at == clock.value

    with pytest.raises(HostBusy) as refused:
        lim.acquire(HOST)
    assert refused.value.reason == "spacing"
    assert refused.value.retry_after == pytest.approx(MIN_SPACING_SECONDS)

    second = lim.acquire(HOST, wait=True, sleep=clock.sleeper())
    assert second.started_at - first.started_at >= MIN_SPACING_SECONDS
    assert second.started_at == pytest.approx(first.started_at + MIN_SPACING_SECONDS)
    assert clock.naps == [pytest.approx(MIN_SPACING_SECONDS)]
    first.release()
    second.release()


def test_third_concurrent_request_on_same_host_is_refused(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    first = lim.acquire(HOST)
    clock.advance(MIN_SPACING_SECONDS)
    second = lim.acquire(HOST)
    assert lim.in_flight(HOST) == 2

    with pytest.raises(HostBusy) as refused:
        lim.acquire(HOST)
    assert refused.value.reason == "concurrent"

    first.release()
    clock.advance(MIN_SPACING_SECONDS)
    third = lim.acquire(HOST)
    assert lim.in_flight(HOST) == 2
    third.release()
    second.release()
    assert lim.in_flight(HOST) == 0


def test_wait_mode_holds_until_a_permit_is_released_then_respects_spacing(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    first = lim.acquire(HOST)
    clock.advance(MIN_SPACING_SECONDS)
    second = lim.acquire(HOST)

    def sleeper(delay: float) -> None:
        clock.advance(delay)
        first.release()

    third = lim.acquire(HOST, wait=True, sleep=sleeper)
    assert third.started_at - second.started_at >= MIN_SPACING_SECONDS
    assert first.released is True
    assert second.released is False
    second.release()
    third.release()
    assert lim.in_flight(HOST) == 0


def test_different_hosts_do_not_block_each_other(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    on_host = lim.acquire(HOST)
    on_other = lim.acquire(OTHER)
    assert on_other.started_at == on_host.started_at == clock.value

    clock.advance(MIN_SPACING_SECONDS)
    second_on_host = lim.acquire(HOST)
    with pytest.raises(HostBusy) as refused:
        lim.acquire(HOST)
    assert refused.value.reason == "concurrent"
    third_on_other = lim.acquire(OTHER)
    assert third_on_other.started_at == clock.value

    on_host.release()
    second_on_host.release()
    lim.record_block(OTHER, code=429)
    lim.record_block(OTHER, code=429)
    assert lim.is_blocked(OTHER) is True
    remaining = lim.remaining(OTHER)
    assert 0.0 < remaining <= BACKOFF_SECONDS
    clock.advance(MIN_SPACING_SECONDS)
    assert lim.is_blocked(HOST) is False
    fresh_on_host = lim.acquire(HOST)
    fresh_on_host.release()
    with pytest.raises(HostBlocked):
        lim.acquire(OTHER)
    on_other.release()
    third_on_other.release()
    assert lim.in_flight(OTHER) == 0


def test_two_consecutive_blocks_open_1800s_window_and_acquire_refuses(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    lim.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015")
    lim.record_block(HOST, code=429)
    assert lim.is_blocked(HOST) is True
    assert lim.remaining(HOST) == pytest.approx(BACKOFF_SECONDS)

    with pytest.raises(HostBlocked) as refused:
        lim.acquire(HOST)
    assert refused.value.host == HOST
    assert refused.value.remaining_seconds == pytest.approx(BACKOFF_SECONDS)
    with pytest.raises(HostBlocked):
        lim.acquire(HOST, wait=True, sleep=clock.sleeper())
    with pytest.raises(HostBlocked):
        lim.slot(HOST)
    assert clock.naps == []
    assert lim.in_flight(HOST) == 0

    entry = agent_state.snapshot()["hosts"][HOST]
    assert entry["consecutive_blocks"] == 2
    assert entry["last_block_code"] == "429"


def test_record_success_clears_the_consecutive_block_counter(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    lim.record_block(HOST, code=429)
    lim.record_success(HOST)
    lim.record_block(HOST, code=503)
    assert lim.is_blocked(HOST) is False
    assert lim.remaining(HOST) == 0.0
    entry = agent_state.snapshot()["hosts"][HOST]
    assert entry["consecutive_blocks"] == 1
    assert entry["last_success_at"] is not None
    permit = lim.acquire(HOST)
    permit.release()


def test_window_expires_once_the_injected_clock_passes_1800s(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    lim.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015")
    lim.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015")
    clock.advance(BACKOFF_SECONDS - 1.0)
    assert lim.is_blocked(HOST) is True
    assert lim.remaining(HOST) == pytest.approx(1.0)
    with pytest.raises(HostBlocked):
        lim.acquire(HOST)
    clock.advance(1.0)
    assert lim.is_blocked(HOST) is False
    assert lim.remaining(HOST) == 0.0
    permit = lim.acquire(HOST)
    permit.release()


def test_fresh_limiter_over_the_same_state_root_still_reports_blocked(clock, tmp_path):
    first = RateLimiter(now=clock, state=AgentState("11", root=tmp_path))
    first.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015")
    first.record_block(HOST, code="ERR_WAF_CLOUDFLARE_1015")

    clock.advance(60.0)
    restarted = RateLimiter(now=clock, state=AgentState("11", root=tmp_path))
    assert restarted.is_blocked(HOST) is True
    assert restarted.remaining(HOST) == pytest.approx(BACKOFF_SECONDS - 60.0)
    with pytest.raises(HostBlocked):
        restarted.acquire(HOST)
    assert (tmp_path / "11.json").is_file()


def test_no_real_time_sleep_is_called_anywhere_in_the_politeness_flow(clock, agent_state, no_real_sleep):
    lim = RateLimiter(now=clock, state=agent_state)
    first = lim.acquire(HOST)
    lim.acquire(HOST, wait=True, sleep=clock.sleeper())
    with pytest.raises(HostBusy):
        lim.acquire(HOST)
    lim.record_block(HOST, code=429)
    lim.record_block(HOST, code=429)
    with pytest.raises(HostBlocked):
        lim.acquire(HOST, wait=True, sleep=clock.sleeper())
    first.release()
    assert no_real_sleep.calls == []


def test_async_acquire_mirrors_sync_and_never_uses_the_default_sleeper(clock, agent_state):
    async def scenario() -> None:
        lim = RateLimiter(now=clock, state=agent_state)
        first = await lim.acquire_async(HOST)
        with pytest.raises(HostBusy) as refused:
            await lim.acquire_async(HOST)
        assert refused.value.reason == "spacing"
        second = await lim.acquire_async(HOST, wait=True, sleep=clock.async_sleeper())
        assert second.started_at - first.started_at >= MIN_SPACING_SECONDS
        lim.record_block(HOST, code=429)
        lim.record_block(HOST, code=429)
        with pytest.raises(HostBlocked):
            await lim.acquire_async(HOST, wait=True, sleep=clock.async_sleeper())
        first.release()
        second.release()

    asyncio.run(scenario())
    assert clock.naps == [pytest.approx(MIN_SPACING_SECONDS)]


def test_slot_context_manager_releases_on_exit_even_on_error(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    with lim.slot(HOST) as permit:
        assert lim.in_flight(HOST) == 1
        assert permit.released is False
    assert lim.in_flight(HOST) == 0
    assert permit.released is True

    clock.advance(MIN_SPACING_SECONDS)
    with pytest.raises(RuntimeError):
        with lim.slot(HOST):
            raise RuntimeError("boom")
    assert lim.in_flight(HOST) == 0


def test_permit_release_is_idempotent_and_release_is_host_scoped(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    first = lim.acquire(HOST)
    other = lim.acquire(OTHER)
    first.release()
    first.release()
    assert lim.in_flight(HOST) == 0
    assert lim.in_flight(OTHER) == 1
    lim.release(HOST)
    assert lim.in_flight(HOST) == 0
    other.release()
    assert lim.in_flight(OTHER) == 0


def test_spacing_is_measured_from_request_start_not_from_release(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    first = lim.acquire(HOST)
    first.release()
    with pytest.raises(HostBusy) as refused:
        lim.acquire(HOST)
    assert refused.value.reason == "spacing"
    assert refused.value.retry_after == pytest.approx(MIN_SPACING_SECONDS)


def test_is_blocking_classifies_429_5xx_and_cloudflare_1015():
    assert is_blocking(429) is True
    assert is_blocking(500) is True
    assert is_blocking(599) is True
    assert is_blocking(1015) is True
    assert is_blocking("ERR_WAF_CLOUDFLARE_1015") is True
    assert is_blocking("err_waf_cloudflare_1015") is True
    assert is_blocking("429") is True
    assert is_blocking("503") is True
    assert is_blocking("5XX") is True
    assert is_blocking(200) is False
    assert is_blocking(403) is False
    assert is_blocking(418) is False
    assert is_blocking(None) is False
    assert is_blocking(True) is False
    assert is_blocking("ERR_CONSENT_ROTATING_LABELS") is False


def test_record_block_normalizes_int_1015_and_rejects_non_blocking_codes(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    lim.record_block(HOST, code=1015)
    assert agent_state.snapshot()["hosts"][HOST]["last_block_code"] == "ERR_WAF_CLOUDFLARE_1015"
    with pytest.raises(ValueError):
        lim.record_block(HOST, code="ERR_CONSENT_ROTATING_LABELS")
    with pytest.raises(ValueError):
        lim.record_block(HOST, code=403)
    assert agent_state.snapshot()["hosts"][HOST]["consecutive_blocks"] == 1


def test_constructor_defaults_and_validation(clock, agent_state):
    lim = RateLimiter(now=clock, state=agent_state)
    assert lim.spacing == MIN_SPACING_SECONDS
    assert lim.max_concurrent == MAX_CONCURRENT_PER_HOST
    shared = RateLimiter(now=clock)
    assert shared.state.path == Path("data/logs/agent_state") / "shared.json"
    with pytest.raises(ValueError):
        RateLimiter(now=clock, state=agent_state, spacing=0.0)
    with pytest.raises(ValueError):
        RateLimiter(now=clock, state=agent_state, max_concurrent=0)
    with pytest.raises(TypeError):
        RateLimiter(now="not-callable", state=agent_state)
