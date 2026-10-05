"""T1: strategy ladder orchestration (SPEC §5 step 3) - first non-empty rung
wins, WAF/429/1015 blocks stop the ladder immediately, an open limiter breaker
short-circuits before any rung, and every attempt is recorded with its
taxonomy code. All tests use a fake ``discover``; zero network."""

from __future__ import annotations

import zipfile

from src.agents.strategies import (
    STRATEGY_INFO,
    STRATEGY_LADDER,
    StrategyResult,
    run_ladder,
)
from src.agents.taxonomy import FAILURE_CODES, is_valid_code


def test_ladder_names_in_documented_order():
    assert STRATEGY_LADDER == (
        "fast_http",
        "curl_impersonate",
        "playwright_token_intercept_then_api",
        "direct_api",
    )
    assert set(STRATEGY_INFO) == set(STRATEGY_LADDER)
    for name in STRATEGY_LADDER:
        assert STRATEGY_INFO[name].strip()


def test_first_rung_success_short_circuits():
    calls: list[str] = []

    def discover(strategy: str, amc_name: str) -> list:
        calls.append(strategy)
        return ["https://amc.example/monthly.pdf"] if strategy == "fast_http" else []

    result = run_ladder("Test AMC", discover=discover)

    assert isinstance(result, StrategyResult)
    assert result.success is True
    assert result.strategy == "fast_http"
    assert result.links == ["https://amc.example/monthly.pdf"]
    assert result.failure_code is None
    assert result.blocked is False
    assert calls == ["fast_http"]
    assert [a["strategy"] for a in result.attempts] == ["fast_http"]
    assert result.attempts[0]["ok"] is True


def test_first_rung_failure_falls_through_to_second():
    def discover(strategy: str, amc_name: str) -> list:
        return [] if strategy == "fast_http" else ["https://amc.example/factsheet.pdf"]

    result = run_ladder("Test AMC", discover=discover)

    assert result.success is True
    assert result.strategy == "curl_impersonate"
    assert result.links == ["https://amc.example/factsheet.pdf"]
    assert [a["strategy"] for a in result.attempts] == ["fast_http", "curl_impersonate"]
    assert result.attempts[0]["ok"] is False
    assert result.attempts[0]["failure_code"] is None
    assert result.attempts[1]["ok"] is True


def test_skip_excludes_rung():
    seen: list[str] = []

    def discover(strategy: str, amc_name: str) -> list:
        seen.append(strategy)
        return []

    result = run_ladder("Test AMC", discover=discover, skip=("fast_http",))

    assert "fast_http" not in seen
    assert [a["strategy"] for a in result.attempts] == [
        "curl_impersonate",
        "playwright_token_intercept_then_api",
        "direct_api",
    ]
    assert result.success is False


def test_block_code_sets_blocked_and_stops_after_one_rung():
    calls: list[str] = []

    def discover(strategy: str, amc_name: str) -> list:
        calls.append(strategy)
        raise RuntimeError("error code: 1015 from cloudflare")

    result = run_ladder("Test AMC", discover=discover)

    assert result.blocked is True
    assert result.success is False
    assert result.failure_code == "ERR_WAF_CLOUDFLARE_1015"
    assert calls == ["fast_http"]
    assert len(result.attempts) == 1
    assert result.attempts[0]["strategy"] == "fast_http"
    assert result.attempts[0]["ok"] is False
    assert result.attempts[0]["failure_code"] == "ERR_WAF_CLOUDFLARE_1015"


def test_every_attempt_is_recorded():
    def discover(strategy: str, amc_name: str) -> list:
        return []

    result = run_ladder("Test AMC", discover=discover)

    assert [a["strategy"] for a in result.attempts] == list(STRATEGY_LADDER)
    assert all(set(a) == {"strategy", "ok", "failure_code", "elapsed"} for a in result.attempts)
    assert all(a["ok"] is False for a in result.attempts)
    assert all(a["failure_code"] is None for a in result.attempts)
    assert all(isinstance(a["elapsed"], float) and a["elapsed"] >= 0.0 for a in result.attempts)
    assert result.success is False
    assert result.blocked is False


def test_limiter_blocked_returns_before_any_discover_call():
    checked_hosts: list[str] = []

    class FakeLimiter:
        def is_blocked(self, host: str) -> bool:
            checked_hosts.append(host)
            return True

    def discover(strategy: str, amc_name: str) -> list:
        raise AssertionError("discover must never run against a circuit-broken host")

    result = run_ladder("Test AMC", discover=discover, limiter=FakeLimiter())

    assert checked_hosts == ["Test AMC"]
    assert result.blocked is True
    assert result.success is False
    assert result.attempts == []
    assert result.links == []
    assert result.failure_code == "ERR_WAF_CLOUDFLARE_1015"


def test_unmappable_exception_yields_valid_default_code():
    def discover(strategy: str, amc_name: str) -> list:
        raise ValueError("boom")

    result = run_ladder("Test AMC", discover=discover)

    assert result.success is False
    assert result.blocked is False
    assert result.failure_code is not None
    assert result.failure_code in FAILURE_CODES
    assert is_valid_code(result.failure_code)
    assert all(a["failure_code"] is None for a in result.attempts)


def test_last_classified_code_wins_when_ladder_exhausts():
    def discover(strategy: str, amc_name: str) -> list:
        if strategy == "fast_http":
            raise zipfile.BadZipFile("not a zip")
        return []

    result = run_ladder("Test AMC", discover=discover)

    assert result.success is False
    assert result.blocked is False
    assert result.failure_code == "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    assert result.attempts[0]["failure_code"] == "ERR_ARCHIVE_ZIP_SINGLE_XLS"
    assert result.attempts[0]["ok"] is False
    assert [a["strategy"] for a in result.attempts] == list(STRATEGY_LADDER)
