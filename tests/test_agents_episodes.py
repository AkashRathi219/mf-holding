"""[T2] Episode journal tests (SPEC §5 step 5 / §7, PLAN §4.1 / AC-5, AC-16).

Covers the append-only JSONL journal: every stored record carries the full §7
schema (``trigger``/``channel`` default to ``None`` and are preserved when
supplied), dedupe folds by ``episode_id`` last-write-wins so re-runs never
double-count, the AC-5 validation gate rejects unclassified failures
(``FAILED``/``PARTIAL`` without a code, unknown codes, a code on SUCCESS),
append safety repairs a stale partial tail before appending, two appends give
two parseable lines with ``count() == 2``, and tmp-path isolation keeps the
real ``data/logs/agent_episodes/`` tree untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.agents.episodes import (
    DEFAULT_ROOT,
    EPISODE_SCHEMA_KEYS,
    OUTCOMES,
    EpisodeJournal,
    count,
    iter_episodes,
    load,
    month_of,
    new_episode_id,
    safe_amc,
)
from src.agents.taxonomy import FAILURE_CODES

FIXED_TS = "2026-10-04T06:01:22+00:00"
MONTH_SHARD = "2026-10"
FILE_NAME = "53_Axis_Mutual_Fund.jsonl"
EPISODE_ID = "ep_53_0001_playwright_token_intercept_then_api"


def _journal(tmp_path: Path) -> EpisodeJournal:
    return EpisodeJournal(root=tmp_path / "agent_episodes")


def _month_file(tmp_path: Path) -> Path:
    return tmp_path / "agent_episodes" / MONTH_SHARD / FILE_NAME


def _episode(**overrides):
    episode = {
        "timestamp": FIXED_TS,
        "episode_id": EPISODE_ID,
        "mf_id": "53",
        "amc_name": "Axis Mutual Fund",
        "fingerprint": {"cdn": "akamai", "cms": "nextjs", "auth": "bearer_jwt"},
        "strategy_applied": "playwright_token_intercept_then_api",
        "tools_used": ["playwright_chromium", "httpx"],
        "outcome": "SUCCESS",
        "failure_code": None,
        "reward": 14.0,
        "discovered_count": 12,
        "downloaded_count": 12,
        "evidence": {
            "sample_url": "https://www.axismf.com/cms/product/factsheet",
            "elapsed_sec": 4.2,
        },
    }
    episode.update(overrides)
    return episode


# ---------------------------------------------------------------------------
# constants and pure helpers
# ---------------------------------------------------------------------------

def test_outcomes_constant_matches_spec():
    assert OUTCOMES == ("SUCCESS", "PARTIAL", "FAILED")


def test_safe_amc_maps_spaces_and_slashes():
    assert safe_amc("Axis Mutual Fund") == "Axis_Mutual_Fund"
    assert safe_amc("360 ONE/Advisorkhoj") == "360_ONE-Advisorkhoj"
    assert safe_amc("  Nippon India  ") == "Nippon_India"


def test_month_of_extracts_shard_and_rejects_garbage():
    assert month_of(FIXED_TS) == MONTH_SHARD
    assert month_of("2026-10-04T06:01:22+05:30") == "2026-10"
    with pytest.raises(ValueError):
        month_of("not-a-timestamp")


def test_new_episode_id_is_deterministic_with_injected_now():
    moment = datetime(2026, 10, 4, 6, 1, 22, tzinfo=timezone.utc)
    expected_ms = int(moment.timestamp() * 1000)
    assert new_episode_id("53", "fast http", now=moment) == f"ep_53_{expected_ms}_fast_http"
    assert (
        new_episode_id("53", "fast_http", now="2026-10-04T06:01:22+00:00")
        == f"ep_53_{expected_ms}_fast_http"
    )


# ---------------------------------------------------------------------------
# 1. append then load returns the episode with ALL schema keys present
# ---------------------------------------------------------------------------

def test_append_then_load_returns_episode_with_all_schema_keys(tmp_path):
    journal = _journal(tmp_path)
    path = journal.append(_episode())
    assert path == _month_file(tmp_path)
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    record = load(path)[EPISODE_ID]
    assert set(record) == set(EPISODE_SCHEMA_KEYS)
    assert record["timestamp"] == FIXED_TS
    assert record["episode_id"] == EPISODE_ID
    assert record["mf_id"] == "53"
    assert record["amc_name"] == "Axis Mutual Fund"
    assert record["fingerprint"] == {"cdn": "akamai", "cms": "nextjs", "auth": "bearer_jwt"}
    assert record["strategy_applied"] == "playwright_token_intercept_then_api"
    assert record["tools_used"] == ["playwright_chromium", "httpx"]
    assert record["outcome"] == "SUCCESS"
    assert record["failure_code"] is None
    assert record["reward"] == 14.0
    assert record["discovered_count"] == 12
    assert record["downloaded_count"] == 12
    assert record["evidence"] == {
        "sample_url": "https://www.axismf.com/cms/product/factsheet",
        "elapsed_sec": 4.2,
    }
    assert json.loads(raw) == record


def test_stored_record_is_schema_exact_and_drops_unknown_keys(tmp_path):
    journal = _journal(tmp_path)
    journal.append(_episode(notes="extra field"))
    record = json.loads(_month_file(tmp_path).read_text(encoding="utf-8"))
    assert set(record) == set(EPISODE_SCHEMA_KEYS)
    assert "notes" not in record


# ---------------------------------------------------------------------------
# 2. trigger/channel default to None and are preserved when supplied
# ---------------------------------------------------------------------------

def test_trigger_and_channel_default_to_none(tmp_path):
    journal = _journal(tmp_path)
    journal.append(_episode())
    record = load(_month_file(tmp_path))[EPISODE_ID]
    assert record["trigger"] is None
    assert record["channel"] is None


def test_trigger_and_channel_preserved_when_supplied(tmp_path):
    journal = _journal(tmp_path)
    episode = _episode(
        episode_id="ep_53_0002_amc_recheck",
        outcome="FAILED",
        failure_code="ERR_HOLDINGS_INCOMPLETE_SUM",
        trigger="escalation",
        channel="amc_recheck",
    )
    episode.pop("timestamp")
    path = journal.append(episode, now=FIXED_TS)
    record = load(path)["ep_53_0002_amc_recheck"]
    assert record["trigger"] == "escalation"
    assert record["channel"] == "amc_recheck"
    assert record["timestamp"] == FIXED_TS


# ---------------------------------------------------------------------------
# 3. appending the SAME episode_id twice folds to ONE record (dedupe)
# ---------------------------------------------------------------------------

def test_same_episode_id_folds_to_one_record(tmp_path):
    journal = _journal(tmp_path)
    journal.append(_episode())
    journal.append(_episode(reward=10.0, downloaded_count=11))
    path = _month_file(tmp_path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    episodes = load(path)
    assert list(episodes) == [EPISODE_ID]
    assert episodes[EPISODE_ID]["reward"] == 10.0
    assert episodes[EPISODE_ID]["downloaded_count"] == 11
    assert count(path) == 1


# ---------------------------------------------------------------------------
# 4/5/6. AC-5 validation gate: outcome/failure_code combinations
# ---------------------------------------------------------------------------

def test_failed_and_partial_without_failure_code_are_rejected(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.append(_episode(outcome="FAILED", failure_code=None))
    with pytest.raises(ValueError):
        journal.append(_episode(outcome="PARTIAL", failure_code=None))
    assert not _month_file(tmp_path).exists()


def test_unknown_failure_code_is_rejected_and_valid_code_accepted(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.append(_episode(outcome="FAILED", failure_code="ERR_TOTALLY_UNKNOWN"))
    with pytest.raises(ValueError):
        journal.append(_episode(outcome="PARTIAL", failure_code="err_waf_cloudflare_1015"))
    path = journal.append(_episode(outcome="FAILED", failure_code="ERR_WAF_CLOUDFLARE_1015"))
    record = load(path)[EPISODE_ID]
    assert record["outcome"] == "FAILED"
    assert record["failure_code"] == "ERR_WAF_CLOUDFLARE_1015"
    assert record["failure_code"] in FAILURE_CODES


def test_success_episode_with_failure_code_is_rejected(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.append(_episode(outcome="SUCCESS", failure_code="ERR_WAF_CLOUDFLARE_1015"))
    assert not _month_file(tmp_path).exists()


def test_unknown_outcome_is_rejected(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.append(_episode(outcome="MAYBE"))
    assert not _month_file(tmp_path).exists()


def test_missing_required_key_is_rejected(tmp_path):
    journal = _journal(tmp_path)
    episode = _episode()
    episode.pop("reward")
    with pytest.raises(ValueError):
        journal.append(episode)
    assert not _month_file(tmp_path).exists()


# ---------------------------------------------------------------------------
# 7. append safety: a stale partial tail is repaired, never poisons the record
# ---------------------------------------------------------------------------

def test_append_repairs_partial_tail_without_corruption(tmp_path):
    journal = _journal(tmp_path)
    path = _month_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"episode_id": "ep_stale", "outcome": "FAI', encoding="utf-8")
    journal.append(_episode())
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    lines = raw.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["episode_id"] == EPISODE_ID
    episodes = load(path)
    assert list(episodes) == [EPISODE_ID]
    assert count(path) == 1


def test_append_terminates_complete_line_missing_newline(tmp_path):
    journal = _journal(tmp_path)
    path = _month_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_episode(episode_id="ep_earlier")), encoding="utf-8")
    journal.append(_episode(episode_id="ep_later"))
    episodes = load(path)
    assert set(episodes) == {"ep_earlier", "ep_later"}
    assert path.read_text(encoding="utf-8").endswith("\n")


# ---------------------------------------------------------------------------
# 8. two sequential appends -> exactly 2 parseable lines, count() == 2
# ---------------------------------------------------------------------------

def test_two_appends_give_two_parseable_lines_and_count_two(tmp_path):
    journal = _journal(tmp_path)
    journal.append(_episode(episode_id="ep_a"))
    journal.append(
        _episode(episode_id="ep_b", outcome="FAILED", failure_code="ERR_AUTH_BEARER_CMS")
    )
    path = _month_file(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [record["episode_id"] for record in parsed] == ["ep_a", "ep_b"]
    assert count(path) == 2
    assert [record["episode_id"] for record in iter_episodes(path)] == ["ep_a", "ep_b"]
    assert list(load(path)) == ["ep_a", "ep_b"]


# ---------------------------------------------------------------------------
# journal class API and defensive readers
# ---------------------------------------------------------------------------

def test_journal_methods_delegate_to_module_helpers(tmp_path):
    journal = _journal(tmp_path)
    path = journal.append(_episode())
    assert journal.path_for("53", "Axis Mutual Fund", MONTH_SHARD) == _month_file(tmp_path)
    assert list(journal.load(path)) == [EPISODE_ID]
    assert journal.count(path) == 1
    assert [record["episode_id"] for record in journal.iter_episodes(path)] == [EPISODE_ID]


def test_load_missing_file_is_empty(tmp_path):
    assert load(tmp_path / "missing.jsonl") == {}
    assert count(tmp_path / "missing.jsonl") == 0
    assert list(iter_episodes(tmp_path / "missing.jsonl")) == []


# ---------------------------------------------------------------------------
# 9. tmp-path isolation: the real data/logs/agent_episodes tree is untouched
# ---------------------------------------------------------------------------

def test_real_data_tree_is_never_touched(tmp_path):
    def _fingerprint(root: Path):
        if not root.exists():
            return None
        return sorted(
            (str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")
        )

    before = _fingerprint(DEFAULT_ROOT)
    journal = _journal(tmp_path)
    journal.append(_episode())
    journal.append(
        _episode(episode_id="ep_b", outcome="PARTIAL", failure_code="ERR_HOLDINGS_PARSER_PARTIAL")
    )
    assert _month_file(tmp_path).exists()
    assert _fingerprint(DEFAULT_ROOT) == before
