"""[T4] Bandit policy tests (SPEC §5 step 2 / PLAN §4.2, AC-3 + AC-4).

Covers the epsilon-greedy decision over discrete playbooks: the stable
fingerprint hash (equal across AMCs sharing the CDN/CMS/auth shape, sensitive to
every component), the exact Q-update formula with the 0.0 floor, 100%-greedy
selection at Q >= 5.0 (AC-3), seeded exploration below the trust line, AC-4
cross-agent transfer for unmapped AMCs, the stale-playbook exclusion, the
promotion gate (observations >= 3 and Q >= 5.0, never fired by decay), and
seed-for-seed determinism. Every rng is injected via ``bandit.seeded_rng`` so
the probabilistic paths assert deterministically, and every register is built
in memory - the real ``data/knowledge/`` tree is never touched.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta

import pytest

from src.agents import knowledge
from src.agents.bandit import (
    DEFAULT_STRATEGY,
    EPSILON,
    GAMMA,
    GREEDY_Q_THRESHOLD,
    MIN_PROMOTION_OBSERVATIONS,
    STRATEGY_LADDER,
    fingerprint_hash,
    maybe_promote,
    q_update,
    seeded_rng,
    select_strategy,
    update_from_episode,
)
from src.agents.knowledge import DECAY_FACTOR, DECAY_THRESHOLD_DAYS, empty_register

T0 = "2026-10-04T06:15:00+05:30"

FP_AXIS = {"cdn": "akamai", "cms": "nextjs", "auth": "bearer_jwt"}
FP_CLOUDFLARE = {"cdn": "cloudflare", "cms": "drupal", "auth": "cookie"}


def _playbook(
    key: str,
    *,
    fp_hash: str,
    strategy: object,
    score: float,
    observations: int,
    **extra: object,
) -> dict:
    entry = {
        "amc_name": key,
        "fingerprint_hash": fp_hash,
        "best_strategy": strategy,
        "confidence_score": score,
        "parameters": {},
        "known_quirks": [],
        "observations": observations,
        "last_updated": T0,
        "decay_applied_at": None,
    }
    entry.update(extra)
    return entry


def _register(*entries: tuple[str, dict]) -> dict:
    register = empty_register(now=T0)
    for key, entry in entries:
        register["playbooks"][key] = entry
    return register


def _episode(**overrides: object) -> dict:
    episode = {
        "timestamp": T0,
        "episode_id": "ep_53_0001_fast_http",
        "mf_id": "53",
        "amc_name": "Axis Mutual Fund",
        "fingerprint": dict(FP_AXIS),
        "strategy_applied": "fast_http",
        "tools_used": ["httpx"],
        "outcome": "SUCCESS",
        "failure_code": None,
        "reward": 5.0,
        "discovered_count": 3,
        "downloaded_count": 2,
        "evidence": {"sample_url": "https://www.axismf.com/", "elapsed_sec": 1.0},
    }
    episode.update(overrides)
    return episode


def _decayed_to_floor(score: float, periods: int) -> str:
    moment = datetime.fromisoformat(T0) - timedelta(days=periods * DECAY_THRESHOLD_DAYS)
    return moment.isoformat()


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

def test_constants_match_plan():
    assert EPSILON == 0.10
    assert GAMMA == 0.95
    assert GREEDY_Q_THRESHOLD == 5.0
    assert MIN_PROMOTION_OBSERVATIONS == 3
    assert DEFAULT_STRATEGY == "fast_http"
    assert DEFAULT_STRATEGY == STRATEGY_LADDER[0]
    assert len(STRATEGY_LADDER) == 4
    assert len(set(STRATEGY_LADDER)) == 4
    assert GAMMA == DECAY_FACTOR


# ---------------------------------------------------------------------------
# 1. fingerprint hash: stable across AMCs, sensitive to every component
# ---------------------------------------------------------------------------

def test_fingerprint_hash_is_stable_across_amcs_and_sensitive_to_components():
    axis_a = {"cdn": "akamai", "cms": "nextjs", "auth": "bearer_jwt"}
    axis_b = {"cdn": "akamai", "cms": "nextjs", "auth": "bearer_jwt"}
    assert fingerprint_hash(axis_a) == fingerprint_hash(axis_b)
    assert fingerprint_hash({"auth": "Bearer_JWT", "cms": "NextJS", "cdn": " Akamai "}) == fingerprint_hash(axis_a)
    assert fingerprint_hash({"cdn": "cloudflare", "cms": "nextjs", "auth": "bearer_jwt"}) != fingerprint_hash(axis_a)
    assert fingerprint_hash({"cdn": "akamai", "cms": "drupal", "auth": "bearer_jwt"}) != fingerprint_hash(axis_a)
    assert fingerprint_hash({"cdn": "akamai", "cms": "nextjs", "auth": "cookie"}) != fingerprint_hash(axis_a)
    digest = fingerprint_hash(axis_a)
    assert len(digest) == 8
    int(digest, 16)
    canonical = "auth=bearer_jwt|cdn=akamai|cms=nextjs"
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    assert fingerprint_hash({}) == fingerprint_hash({"cdn": None, "cms": None, "auth": None})
    assert fingerprint_hash(FP_AXIS) != fingerprint_hash(FP_CLOUDFLARE)


# ---------------------------------------------------------------------------
# 2. Q-update: exact SPEC formula, stable rounding, 0.0 floor
# ---------------------------------------------------------------------------

def test_q_update_matches_spec_formula_and_floors_at_zero():
    assert q_update(0.0, 10.0) == (1 - 0.2) * 0.0 + 0.2 * 10.0 == 2.0
    assert q_update(5.0, 5.0) == (1 - 0.2) * 5.0 + 0.2 * 5.0 == 5.0
    assert q_update(9.4, 14.0) == round((1 - 0.2) * 9.4 + 0.2 * 14.0, 2)
    assert q_update(4.0, -3.0) == round((1 - 0.2) * 4.0 + 0.2 * -3.0, 2)
    assert q_update(2.5, 2.5, alpha=0.5) == 2.5
    assert q_update(1.0, -10.0) == 0.0
    assert q_update(0.0, -5.0) == 0.0
    assert q_update(-4.0, -1.0) == 0.0
    with pytest.raises(ValueError):
        q_update(1.0, 1.0, alpha=0.0)
    with pytest.raises(ValueError):
        q_update(1.0, 1.0, alpha=1.5)


# ---------------------------------------------------------------------------
# 3. AC-3: a playbook at Q >= 5.0 is followed greedily, 100 of 100 calls
# ---------------------------------------------------------------------------

def test_high_q_playbook_is_greedy_in_100_of_100_seeded_calls():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="playwright_token_intercept_then_api",
                score=6.0,
                observations=8,
            ),
        )
    )
    picks = [select_strategy(register, FP_AXIS, rng=seeded_rng(1234)) for _ in range(100)]
    assert picks == ["playwright_token_intercept_then_api"] * 100


def test_greedy_requires_observations_and_holds_at_the_threshold_boundary():
    at_threshold = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="curl_impersonate",
                score=5.0,
                observations=3,
            ),
        )
    )
    assert all(
        select_strategy(at_threshold, FP_AXIS, rng=seeded_rng(7)) == "curl_impersonate"
        for _ in range(50)
    )
    below_threshold = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="curl_impersonate",
                score=4.99,
                observations=3,
            ),
        )
    )
    rng = seeded_rng(7)
    picks = {select_strategy(below_threshold, FP_AXIS, rng=rng) for _ in range(100)}
    assert len(picks) > 1
    assert picks <= set(STRATEGY_LADDER)
    zero_observations = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=9.0,
                observations=0,
            ),
        )
    )
    rng = seeded_rng(8)
    picks = [select_strategy(zero_observations, FP_AXIS, rng=rng) for _ in range(50)]
    assert any(pick != "fast_http" for pick in picks)
    assert picks.count("fast_http") > 35


# ---------------------------------------------------------------------------
# 4. low-Q playbook: exploration happens sometimes (seeded -> deterministic)
# ---------------------------------------------------------------------------

def test_low_q_playbook_explores_over_many_seeded_draws():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=1.0,
                observations=2,
            ),
        )
    )
    rng = seeded_rng(2026)
    picks = [select_strategy(register, FP_AXIS, rng=rng) for _ in range(200)]
    explored = [pick for pick in picks if pick != "fast_http"]
    assert 0 < len(explored) < 200
    assert set(explored) <= set(STRATEGY_LADDER)
    assert 0.05 < len(explored) / len(picks) < 0.16
    again_rng = seeded_rng(2026)
    again = [select_strategy(register, FP_AXIS, rng=again_rng) for _ in range(200)]
    assert again == picks


# ---------------------------------------------------------------------------
# 5. AC-4: an unmapped AMC inherits a matching-fingerprint peer's strategy
# ---------------------------------------------------------------------------

def test_unmapped_amc_inherits_matching_fingerprint_peer_strategy():
    trusted_peer = _register(
        (
            "9",
            _playbook(
                "9",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="playwright_token_intercept_then_api",
                score=7.2,
                observations=9,
            ),
        )
    )
    assert (
        select_strategy(trusted_peer, FP_AXIS, rng=seeded_rng(5))
        == "playwright_token_intercept_then_api"
    )
    low_q_peer = _register(
        (
            "9",
            _playbook(
                "9",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="curl_impersonate",
                score=1.0,
                observations=1,
            ),
        )
    )
    rng = seeded_rng(11)
    picks = [select_strategy(low_q_peer, FP_AXIS, rng=rng) for _ in range(100)]
    assert picks.count("curl_impersonate") >= 80
    assert any(pick != "curl_impersonate" for pick in picks)
    assert select_strategy(_register(), FP_AXIS, rng=seeded_rng(3)) == DEFAULT_STRATEGY


# ---------------------------------------------------------------------------
# 6. a fully decayed (stale) playbook is never greedy-selected
# ---------------------------------------------------------------------------

def test_fully_decayed_playbook_is_not_greedy_selected():
    entry = _playbook(
        "53",
        fp_hash=fingerprint_hash(FP_AXIS),
        strategy="fast_http",
        score=1.0,
        observations=12,
        last_updated=_decayed_to_floor(1.0, 110),
    )
    register = _register(("53", entry))
    knowledge.apply_decay(register, now=T0)
    assert knowledge.is_stale(entry)
    assert entry["best_strategy"] == "fast_http"
    rng = seeded_rng(99)
    picks = [select_strategy(register, FP_AXIS, rng=rng) for _ in range(100)]
    assert any(pick != "fast_http" for pick in picks)
    assert "fast_http" in picks


# ---------------------------------------------------------------------------
# 9. None / absent / "manual" playbooks are never auto-selected
# ---------------------------------------------------------------------------

def test_manual_or_empty_playbooks_are_never_selected():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="manual",
                score=9.9,
                observations=9,
            ),
        ),
        (
            "9",
            _playbook(
                "9",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy=None,
                score=9.9,
                observations=9,
            ),
        ),
    )
    picks = {select_strategy(register, FP_AXIS, rng=seeded_rng(1)) for _ in range(50)}
    assert picks == {DEFAULT_STRATEGY}


def test_select_strategy_does_not_mutate_the_register():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=2.0,
                observations=4,
            ),
        ),
        (
            "9",
            _playbook(
                "9",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="curl_impersonate",
                score=1.5,
                observations=2,
            ),
        ),
    )
    before = copy.deepcopy(register)
    for seed in range(20):
        select_strategy(register, FP_AXIS, rng=seeded_rng(seed))
    assert register == before


# ---------------------------------------------------------------------------
# 8. determinism: same seed + same register -> same selection sequence
# ---------------------------------------------------------------------------

def test_same_seed_and_register_produce_identical_sequences():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=2.0,
                observations=4,
            ),
        ),
        (
            "9",
            _playbook(
                "9",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="curl_impersonate",
                score=1.5,
                observations=2,
            ),
        ),
        (
            "12",
            _playbook(
                "12",
                fp_hash=fingerprint_hash(FP_CLOUDFLARE),
                strategy="direct_api",
                score=8.0,
                observations=6,
            ),
        ),
    )
    first_rng = seeded_rng(42)
    first = [select_strategy(register, FP_AXIS, rng=first_rng) for _ in range(60)]
    second_rng = seeded_rng(42)
    second = [select_strategy(register, FP_AXIS, rng=second_rng) for _ in range(60)]
    assert first == second
    other_rng = seeded_rng(43)
    other = [select_strategy(register, FP_AXIS, rng=other_rng) for _ in range(60)]
    assert other != first
    assert set(first) <= set(STRATEGY_LADDER)


# ---------------------------------------------------------------------------
# update_from_episode: delegation to knowledge + the promotion ledger
# ---------------------------------------------------------------------------

def test_update_from_episode_folds_reward_into_own_playbook():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=3.0,
                observations=3,
            ),
        )
    )
    entry = update_from_episode(register, _episode(reward=5.0), now=T0)
    assert entry["confidence_score"] == round((1 - 0.2) * 3.0 + 0.2 * 5.0, 2) == 3.4
    assert entry["best_strategy"] == "fast_http"
    assert entry["observations"] == 4
    assert entry["last_updated"] == T0
    ledger = entry["parameters"]["strategy_scores"]["fast_http"]
    assert ledger == {"q": 3.4, "observations": 1}
    assert register["version"] == "1.0.1"


def test_failed_episode_never_raises_q_via_the_knowledge_guard():
    base = _playbook(
        "53",
        fp_hash=fingerprint_hash(FP_AXIS),
        strategy="fast_http",
        score=6.0,
        observations=6,
    )
    register = _register(("53", base))
    entry = update_from_episode(
        register,
        _episode(outcome="FAILED", failure_code="ERR_WAF_CLOUDFLARE_1015", reward=10.0),
        now=T0,
    )
    assert entry["confidence_score"] == 6.0
    assert entry["parameters"]["strategy_scores"]["fast_http"]["q"] == 6.0
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=6.0,
                observations=6,
            ),
        )
    )
    entry = update_from_episode(
        register,
        _episode(outcome="FAILED", failure_code="ERR_WAF_CLOUDFLARE_1015", reward=-6.5),
        now=T0,
    )
    assert entry["confidence_score"] == round((1 - 0.2) * 6.0 + 0.2 * -6.5, 2) == 3.5
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=6.0,
                observations=6,
            ),
        )
    )
    entry = update_from_episode(register, _episode(reward=10.0), now=T0)
    assert entry["confidence_score"] == round((1 - 0.2) * 6.0 + 0.2 * 10.0, 2) == 6.8


def test_update_from_episode_creates_playbook_for_unmapped_amc():
    register = _register(
        (
            "9",
            _playbook(
                "9",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="curl_impersonate",
                score=7.0,
                observations=5,
            ),
        )
    )
    entry = update_from_episode(
        register,
        _episode(mf_id="77", amc_name="New AMC", episode_id="ep_77_0001_fast_http", reward=6.0),
        now=T0,
    )
    assert "77" in register["playbooks"]
    assert entry["fingerprint_hash"] == fingerprint_hash(FP_AXIS)
    assert entry["amc_name"] == "New AMC"
    assert entry["best_strategy"] == "fast_http"
    assert entry["confidence_score"] == round(0.2 * 6.0, 2) == 1.2
    assert entry["observations"] == 1
    assert register["version"] == "1.0.1"


def test_update_from_episode_feeds_the_promotion_ledger():
    register = _register(
        (
            "53",
            _playbook(
                "53",
                fp_hash=fingerprint_hash(FP_AXIS),
                strategy="fast_http",
                score=3.0,
                observations=3,
            ),
        )
    )
    for index in range(3):
        update_from_episode(
            register,
            _episode(
                episode_id=f"ep_53_00{index}_curl_impersonate",
                strategy_applied="curl_impersonate",
                reward=14.0,
            ),
            now=T0,
        )
    entry = register["playbooks"]["53"]
    assert entry["best_strategy"] == "fast_http"
    assert entry["confidence_score"] == 3.0
    ledger = entry["parameters"]["strategy_scores"]["curl_impersonate"]
    assert ledger["observations"] == 3
    assert ledger["q"] == round(round(round(0.2 * 14.0, 2) * 0.8 + 0.2 * 14.0, 2) * 0.8 + 0.2 * 14.0, 2)
    assert ledger["q"] == 6.83
    assert maybe_promote(entry, "curl_impersonate") is True
    assert entry["best_strategy"] == "curl_impersonate"


# ---------------------------------------------------------------------------
# 7. maybe_promote: thresholds gate it, decay never triggers it
# ---------------------------------------------------------------------------

def test_maybe_promote_gates_and_never_promotes_on_decay():
    playbook = _playbook(
        "53",
        fp_hash=fingerprint_hash(FP_AXIS),
        strategy="fast_http",
        score=2.0,
        observations=2,
    )
    assert maybe_promote(playbook, "curl_impersonate") is False
    assert playbook["best_strategy"] == "fast_http"
    playbook["parameters"]["strategy_scores"] = {}
    ledger = playbook["parameters"]["strategy_scores"]
    ledger["curl_impersonate"] = {"q": 6.0, "observations": 2}
    assert maybe_promote(playbook, "curl_impersonate") is False
    ledger["curl_impersonate"] = {"q": 4.99, "observations": 5}
    assert maybe_promote(playbook, "curl_impersonate") is False
    ledger["curl_impersonate"] = {"q": 5.0, "observations": 3}
    assert maybe_promote(playbook, "curl_impersonate") is True
    assert playbook["best_strategy"] == "curl_impersonate"
    assert maybe_promote(playbook, "curl_impersonate") is False
    assert maybe_promote(playbook, "manual") is False
    assert maybe_promote(playbook, None) is False
    assert maybe_promote(playbook, "   ") is False
    assert playbook["best_strategy"] == "curl_impersonate"


def test_decay_never_promotes_even_with_a_qualifying_candidate():
    playbook = _playbook(
        "9",
        fp_hash=fingerprint_hash(FP_AXIS),
        strategy="fast_http",
        score=1.0,
        observations=9,
        last_updated=_decayed_to_floor(1.0, 110),
    )
    playbook["parameters"]["strategy_scores"] = {
        "curl_impersonate": {"q": 9.0, "observations": 12},
    }
    register = _register(("9", playbook))
    knowledge.apply_decay(register, now=T0)
    assert knowledge.is_stale(playbook)
    assert playbook["confidence_score"] == 0.0
    assert playbook["best_strategy"] == "fast_http"
    assert maybe_promote(playbook, "direct_api") is False
    assert playbook["best_strategy"] == "fast_http"
