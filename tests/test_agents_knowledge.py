"""T3: shared knowledge register - load/save/version/confidence-decay/MD render.

Covers the PLAN 4.2 contract: round-trip fidelity, string keys for the source
agents (``src_amfi`` / ``src_advisorkhoj`` - never int-coerced), the Q-update
fold, the decay rule (reduces confidence over time with an injected clock and
NEVER resurrects or swaps a stale ``best_strategy``), deterministic Markdown
rendering, empty-register fallback for missing/corrupt files, atomic saves
that leave no temp file behind, and semver bumping. Every test writes only
under ``tmp_path`` - the real ``data/knowledge/`` tree is never touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.agents import knowledge
from src.agents.knowledge import (
    ALPHA,
    CONFIDENCE_FLOOR,
    DECAY_FACTOR,
    DECAY_THRESHOLD_DAYS,
    DEFAULT_JSON_PATH,
    DEFAULT_MD_PATH,
    REGISTER_VERSION,
    apply_decay,
    bump_version,
    empty_register,
    is_stale,
    load,
    render_markdown,
    save,
    update_playbook,
)

T0 = "2026-10-04T06:15:00+05:30"
_T0 = datetime.fromisoformat(T0)
T_PLUS_1D = (_T0 + timedelta(days=1)).isoformat()
T_PLUS_45D = (_T0 + timedelta(days=45)).isoformat()
T_PLUS_75D = (_T0 + timedelta(days=75)).isoformat()
T_PLUS_20Y = (_T0 + timedelta(days=20 * 365)).isoformat()

SCHEMA_FIELDS = {
    "amc_name",
    "fingerprint_hash",
    "best_strategy",
    "confidence_score",
    "parameters",
    "known_quirks",
    "observations",
    "last_updated",
    "decay_applied_at",
}


def _sample_register() -> dict:
    return {
        "version": "1.0.0",
        "updated_at": T0,
        "playbooks": {
            "53": {
                "amc_name": "Axis Mutual Fund",
                "fingerprint_hash": "a9f82d1c",
                "best_strategy": "playwright_token_intercept_then_api",
                "confidence_score": 9.4,
                "parameters": {"token_trigger": "/cms/product/factsheet"},
                "known_quirks": ["Requires Bearer token from live browser context"],
                "observations": 12,
                "last_updated": T0,
                "decay_applied_at": None,
            },
        },
    }


def _multi_playbook_register() -> dict:
    reg = _sample_register()
    reg["playbooks"]["src_amfi"] = {
        "amc_name": "AMFI (source agent)",
        "fingerprint_hash": "amfi_feed",
        "best_strategy": "amfi_nextjs_portfolio_feed",
        "confidence_score": 3.0,
        "parameters": {},
        "known_quirks": [],
        "observations": 2,
        "last_updated": T0,
        "decay_applied_at": None,
    }
    reg["playbooks"]["9"] = {
        "amc_name": "AMC Nine",
        "fingerprint_hash": "b1c2d3e4",
        "best_strategy": "fast_http",
        "confidence_score": 6.1,
        "parameters": {},
        "known_quirks": ["Consent | labels rotate"],
        "observations": 7,
        "last_updated": T0,
        "decay_applied_at": None,
    }
    return reg


def _data_rows(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith(("| Key", "| ---"))
    ]


def test_default_paths_and_constants_match_plan():
    assert DEFAULT_JSON_PATH == Path("data/knowledge/amc_playbooks.json")
    assert DEFAULT_MD_PATH == Path("data/knowledge/AMC_PLAYBOOKS.md")
    assert REGISTER_VERSION == "1.0.0"
    assert DECAY_THRESHOLD_DAYS == 30
    assert DECAY_FACTOR == 0.95
    assert CONFIDENCE_FLOOR == 0.0
    assert ALPHA == 0.2


def test_round_trip_preserves_all_fields(tmp_path):
    reg = _sample_register()
    json_path = tmp_path / "amc_playbooks.json"
    md_path = tmp_path / "AMC_PLAYBOOKS.md"
    save(reg, json_path, md_path=md_path, now=T0)
    loaded = load(json_path, now=T_PLUS_1D)
    assert loaded == reg
    pb = loaded["playbooks"]["53"]
    assert pb["amc_name"] == "Axis Mutual Fund"
    assert pb["fingerprint_hash"] == "a9f82d1c"
    assert pb["best_strategy"] == "playwright_token_intercept_then_api"
    assert pb["confidence_score"] == 9.4
    assert pb["parameters"] == {"token_trigger": "/cms/product/factsheet"}
    assert pb["known_quirks"] == ["Requires Bearer token from live browser context"]
    assert pb["observations"] == 12
    assert pb["last_updated"] == T0
    assert pb["decay_applied_at"] is None
    assert loaded["version"] == "1.0.0"
    assert loaded["updated_at"] == T0


def test_source_agent_string_keys_round_trip_without_int_coercion(tmp_path):
    reg = _sample_register()
    reg["playbooks"]["src_amfi"] = {
        "amc_name": "AMFI (source agent)",
        "fingerprint_hash": "amfi_feed",
        "best_strategy": "amfi_nextjs_portfolio_feed",
        "confidence_score": 3.0,
        "parameters": {"base_url": "https://www.amfiindia.com"},
        "known_quirks": [],
        "observations": 2,
        "last_updated": T0,
        "decay_applied_at": None,
    }
    reg["playbooks"]["src_advisorkhoj"] = {
        "amc_name": "Advisorkhoj (source agent)",
        "fingerprint_hash": "advisorkhoj_html",
        "best_strategy": "advisorkhoj_portfolio_crawl",
        "confidence_score": 1.5,
        "parameters": {},
        "known_quirks": ["Lower trust: republished AMC data"],
        "observations": 1,
        "last_updated": T0,
        "decay_applied_at": None,
    }
    json_path = tmp_path / "amc_playbooks.json"
    save(reg, json_path, now=T0)
    loaded = load(json_path, now=T_PLUS_1D)
    assert set(loaded["playbooks"]) == {"53", "src_amfi", "src_advisorkhoj"}
    assert all(isinstance(key, str) for key in loaded["playbooks"])
    assert loaded["playbooks"]["src_amfi"]["confidence_score"] == 3.0
    assert loaded["playbooks"]["src_advisorkhoj"]["known_quirks"] == ["Lower trust: republished AMC data"]

    pb = update_playbook(
        loaded, "src_amfi", strategy="amfi_nextjs_portfolio_feed", reward=5.0, now=T_PLUS_1D
    )
    assert pb["confidence_score"] == round((1 - ALPHA) * 3.0 + ALPHA * 5.0, 2)
    assert pb["observations"] == 3
    assert set(loaded["playbooks"]) == {"53", "src_amfi", "src_advisorkhoj"}

    int_keyed = update_playbook(
        loaded, 53, strategy="playwright_token_intercept_then_api", reward=1.0, now=T_PLUS_1D
    )
    assert int_keyed is loaded["playbooks"]["53"]
    assert all(isinstance(key, str) for key in loaded["playbooks"])


def test_update_playbook_folds_reward_and_increments_observations():
    reg = _sample_register()
    pb = update_playbook(
        reg, "53", strategy="playwright_token_intercept_then_api", reward=10.0, now=T0
    )
    assert pb["confidence_score"] == round((1 - ALPHA) * 9.4 + ALPHA * 10.0, 2)
    assert pb["confidence_score"] == 9.52
    assert pb["observations"] == 13
    assert pb["last_updated"] == T0
    assert reg["version"] == "1.0.1"


def test_failed_episodes_only_lower_confidence():
    reg = _sample_register()
    reg["playbooks"]["53"]["confidence_score"] = 2.0
    pb = update_playbook(
        reg,
        "53",
        strategy="playwright_token_intercept_then_api",
        reward=10.0,
        outcome="FAILED",
        now=T0,
    )
    assert pb["confidence_score"] == 2.0
    ok = update_playbook(
        reg,
        "53",
        strategy="playwright_token_intercept_then_api",
        reward=10.0,
        outcome="SUCCESS",
        now=T0,
    )
    assert ok["confidence_score"] == round((1 - ALPHA) * 2.0 + ALPHA * 10.0, 2)


def test_update_with_different_strategy_never_moves_best_strategy_or_score():
    reg = _sample_register()
    pb = update_playbook(reg, "53", strategy="fast_http", reward=10.0, now=T0)
    assert pb["best_strategy"] == "playwright_token_intercept_then_api"
    assert pb["confidence_score"] == 9.4
    assert pb["observations"] == 13


def test_update_playbook_creates_missing_entries_for_source_agents():
    reg = empty_register(now=T0)
    pb = update_playbook(
        reg,
        "src_advisorkhoj",
        strategy="advisorkhoj_portfolio_crawl",
        reward=4.0,
        now=T0,
        amc_name="Advisorkhoj (source agent)",
        fingerprint_hash="advisorkhoj_html",
    )
    assert reg["playbooks"]["src_advisorkhoj"] is pb
    assert set(pb) == SCHEMA_FIELDS
    assert pb["best_strategy"] == "advisorkhoj_portfolio_crawl"
    assert pb["confidence_score"] == round(ALPHA * 4.0, 2)
    assert pb["observations"] == 1
    assert pb["amc_name"] == "Advisorkhoj (source agent)"
    assert pb["fingerprint_hash"] == "advisorkhoj_html"
    assert pb["last_updated"] == T0
    assert pb["decay_applied_at"] is None


def test_decay_reduces_confidence_over_time_with_injected_now():
    reg = _sample_register()
    apply_decay(reg, now=T_PLUS_45D)
    pb = reg["playbooks"]["53"]
    assert pb["confidence_score"] == round(9.4 * DECAY_FACTOR, 2)
    assert pb["confidence_score"] == 8.93
    assert pb["decay_applied_at"] == T_PLUS_45D
    assert pb["best_strategy"] == "playwright_token_intercept_then_api"

    fresh = _sample_register()
    apply_decay(fresh, now=T_PLUS_1D)
    assert fresh["playbooks"]["53"]["confidence_score"] == 9.4
    assert fresh["playbooks"]["53"]["decay_applied_at"] is None

    two_periods = _sample_register()
    apply_decay(two_periods, now=T_PLUS_75D)
    assert two_periods["playbooks"]["53"]["confidence_score"] == 8.48
    assert two_periods["playbooks"]["53"]["confidence_score"] < 8.93


def test_decay_applies_on_load(tmp_path):
    json_path = tmp_path / "amc_playbooks.json"
    save(_sample_register(), json_path, now=T0)
    loaded = load(json_path, now=T_PLUS_45D)
    assert loaded["playbooks"]["53"]["confidence_score"] == 8.93
    assert loaded["playbooks"]["53"]["decay_applied_at"] == T_PLUS_45D
    no_decay = load(json_path, now=T_PLUS_1D, decay=False)
    assert no_decay["playbooks"]["53"]["confidence_score"] == 9.4
    assert no_decay["playbooks"]["53"]["decay_applied_at"] is None


def test_aggressive_decay_never_resurrects_a_stale_best_strategy(tmp_path):
    json_path = tmp_path / "amc_playbooks.json"
    save(_sample_register(), json_path, now=T0)
    decayed = load(json_path, now=T_PLUS_20Y)
    pb = decayed["playbooks"]["53"]
    assert pb["best_strategy"] == "playwright_token_intercept_then_api"
    assert pb["confidence_score"] == CONFIDENCE_FLOOR
    assert pb["decay_applied_at"] == T_PLUS_20Y
    assert is_stale(pb) is True

    save(decayed, json_path, now=T_PLUS_20Y)
    reread = load(json_path, now=T_PLUS_20Y)
    assert reread["playbooks"]["53"]["best_strategy"] == "playwright_token_intercept_then_api"
    assert reread["playbooks"]["53"]["confidence_score"] == CONFIDENCE_FLOOR

    after_update = update_playbook(
        decayed, "53", strategy="fast_http", reward=10.0, now=T_PLUS_20Y
    )
    assert after_update["best_strategy"] == "playwright_token_intercept_then_api"
    assert after_update["confidence_score"] == CONFIDENCE_FLOOR


def test_render_markdown_is_deterministic_with_one_row_per_playbook():
    reg = _multi_playbook_register()
    first = render_markdown(reg)
    second = render_markdown(reg)
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")

    reordered = {
        "version": reg["version"],
        "updated_at": reg["updated_at"],
        "playbooks": {key: reg["playbooks"][key] for key in ("9", "src_amfi", "53")},
    }
    assert render_markdown(reordered) == first

    rows = _data_rows(first)
    assert len(rows) == 3
    keys_in_order = [row.split("|")[1].strip() for row in rows]
    assert keys_in_order == ["9", "53", "src_amfi"]
    assert "| fast_http |" in first
    assert "never hand-edit" in first
    assert "Register version: `1.0.0`" in first
    assert first.endswith("\n")


def test_render_markdown_escapes_pipes_and_newlines_in_cells():
    reg = _sample_register()
    reg["playbooks"]["53"]["known_quirks"] = ["Consent | labels rotate", "Multi\nline quirk"]
    text = render_markdown(reg)
    assert "Consent \\| labels rotate" in text
    assert "Multi line quirk" in text
    assert len(_data_rows(text)) == 1


def test_missing_and_corrupt_register_loads_as_empty_without_raising(tmp_path):
    missing = load(tmp_path / "missing.json", now=T0)
    assert missing == {"version": "1.0.0", "updated_at": T0, "playbooks": {}}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json at all", encoding="utf-8")
    assert load(corrupt, now=T0)["playbooks"] == {}

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]", encoding="utf-8")
    loaded_list = load(wrong_shape, now=T0)
    assert loaded_list["playbooks"] == {}
    assert loaded_list["version"] == "1.0.0"

    blank = tmp_path / "blank.json"
    blank.write_text("", encoding="utf-8")
    assert load(blank, now=T0)["playbooks"] == {}


def test_atomic_save_leaves_no_temp_file_behind_and_syncs_md(tmp_path, monkeypatch):
    json_path = tmp_path / "amc_playbooks.json"
    md_path = tmp_path / "AMC_PLAYBOOKS.md"
    reg = _sample_register()
    save(reg, json_path, md_path=md_path, now=T0)
    assert json_path.exists() and md_path.exists()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "AMC_PLAYBOOKS.md",
        "amc_playbooks.json",
    ]
    assert md_path.read_text(encoding="utf-8") == render_markdown(reg)
    assert json.loads(json_path.read_text(encoding="utf-8"))["version"] == "1.0.0"

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(knowledge.os, "replace", _boom)
    with pytest.raises(OSError):
        save(reg, json_path, md_path=md_path, now=T0)
    monkeypatch.undo()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "AMC_PLAYBOOKS.md",
        "amc_playbooks.json",
    ]
    assert json.loads(json_path.read_text(encoding="utf-8"))["playbooks"]["53"]["confidence_score"] == 9.4


def test_save_creates_missing_parent_directories(tmp_path):
    json_path = tmp_path / "deep" / "nested" / "amc_playbooks.json"
    save(_sample_register(), json_path, now=T0)
    assert json_path.exists()
    assert (tmp_path / "deep" / "nested" / "AMC_PLAYBOOKS.md").exists()


def test_load_fills_missing_playbook_fields_with_defaults(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps(
            {"version": "1.0.0", "updated_at": T0, "playbooks": {"7": {"amc_name": "Quant Mutual Fund"}}}
        ),
        encoding="utf-8",
    )
    loaded = load(path, now=T_PLUS_1D)
    pb = loaded["playbooks"]["7"]
    assert set(pb) == SCHEMA_FIELDS
    assert pb["amc_name"] == "Quant Mutual Fund"
    assert pb["confidence_score"] == 0.0
    assert pb["observations"] == 0
    assert pb["best_strategy"] == ""
    assert pb["known_quirks"] == []
    assert pb["parameters"] == {}
    assert pb["last_updated"] is None
    assert pb["decay_applied_at"] is None


def test_bump_version_is_monotonic_and_deterministic():
    reg = _sample_register()
    assert bump_version(reg, "patch") == "1.0.1"
    assert bump_version(reg, "patch") == "1.0.2"
    assert bump_version(reg, "minor") == "1.1.0"
    assert bump_version(reg, "major") == "2.0.0"
    other = _sample_register()
    assert bump_version(other, "patch") == "1.0.1"

    sequence: list[str] = []
    reg2 = _sample_register()
    for level in ("patch", "patch", "minor", "patch", "major", "patch"):
        sequence.append(bump_version(reg2, level))
    parsed = [tuple(int(part) for part in version.split(".")) for version in sequence]
    assert all(a < b for a, b in zip(parsed, parsed[1:]))

    malformed = _sample_register()
    malformed["version"] = "not-a-version"
    assert bump_version(malformed, "patch") == "1.0.1"
    with pytest.raises(ValueError):
        bump_version(malformed, "weekly")
