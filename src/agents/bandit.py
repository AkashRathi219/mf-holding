"""Epsilon-greedy bandit policy over the shared playbook register (SPEC §5 step 2, PLAN §4.2, T4).

Memory-based contextual bandit over DISCRETE playbooks - no neural network and
no ML dependency (SPEC §3 non-goal). The context is the site fingerprint shape
(``cdn``/``cms``/``auth``), the actions are the fixed strategy ladder, and the
value model is the per-playbook ``confidence_score`` that
``src.agents.knowledge`` maintains. All three agent classes (57 discovery
agents, the integrity agent, the two source agents) share this policy and the
same register, which is what makes cross-agent transfer possible.

Decision procedure (:func:`select_strategy`), evaluated in order:

1. GREEDY (AC-3): when a playbook matching the fingerprint has
   ``confidence_score >= GREEDY_Q_THRESHOLD`` (5.0), ``observations > 0`` and is
   not stale (:func:`knowledge.is_stale`), its ``best_strategy`` is returned
   unconditionally - no exploration draw happens at all, so a trusted playbook
   is followed in 100% of calls (AC-3 requires >= 90%).
2. EXPLORE / EXPLOIT: otherwise, with probability ``EPSILON`` (0.10) an
   alternative strategy is drawn - peer-proven strategies for the fingerprint
   first, then the remaining SPEC §5 ladder rungs - and with probability
   ``1 - EPSILON`` the best-scoring matching playbook's ``best_strategy`` is
   returned.
3. TRANSFER (AC-4): selection is fingerprint-driven, so an unmapped AMC
   automatically inherits the top-rated peer strategy for its fingerprint shape
   through rule 2 (and tests alternatives around it through exploration); only
   when no playbook at all shares the shape does the generic
   ``DEFAULT_STRATEGY`` apply.

Playbooks whose ``best_strategy`` is ``None``, absent, empty or ``"manual"`` are
excluded from every selection path: a human-parked MANUAL decision is never
auto-selected by the bandit, greedily or otherwise.

Q-update (:func:`q_update`) implements SPEC §5 step 6,
``Q_{t+1} = (1 - alpha) * Q_t + alpha * R`` with ``alpha = 0.2``, rounded to 2
decimals and floored at 0.0. :func:`update_from_episode` validates the finished
episode with ``episodes.normalize_episode`` and delegates the canonical register
fold to ``knowledge.update_playbook`` - the guards live there (the reward only
moves ``confidence_score`` when it matches ``best_strategy``; a non-SUCCESS
outcome may only lower Q) and are not re-implemented here. On top of the
canonical fold it maintains the bandit's per-strategy promotion ledger.

Promotion (:func:`maybe_promote`) is the bandit's explicit decision, never a
side effect of decay: ``best_strategy`` is swapped only when the candidate
strategy's ledger shows ``observations >= 3`` and ``q >= GREEDY_Q_THRESHOLD``.
The ledger rides inside the playbook's free-form ``parameters`` object under
``strategy_scores`` (``{strategy: {"q": float, "observations": int}}``) so the
PLAN §4.2 playbook keys stay untouched for generic register validators; decay
(``knowledge.apply_decay``) neither reads nor writes the ledger and never
touches ``best_strategy``, so it can never promote.

Determinism: every stochastic choice draws from an injected ``random.Random``
(:func:`seeded_rng` builds one; ``rng=None`` falls back to a module-level
default that is a convenience only and is NOT reproducible across runs).
``GAMMA`` (0.95) is the SPEC §5 discount factor; it is realized as
``knowledge.DECAY_FACTOR``, the per-30-day confidence decay applied on
load/update - the bandit itself performs no additional discounting. There is no
I/O here: callers load and save the register through ``knowledge.load`` /
``knowledge.save``.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping
from datetime import datetime

from src.agents import knowledge
from src.agents.episodes import FINGERPRINT_KEYS, normalize_episode
from src.agents.knowledge import ALPHA, CONFIDENCE_FLOOR
from src.agents.strategies import STRATEGY_LADDER

EPSILON = 0.10
GAMMA = 0.95
GREEDY_Q_THRESHOLD = 5.0
MIN_PROMOTION_OBSERVATIONS = 3
FINGERPRINT_HASH_CHARS = 8
MANUAL_STRATEGY = "manual"
LEDGER_KEY = "strategy_scores"

DEFAULT_STRATEGY = STRATEGY_LADDER[0]

_DEFAULT_RNG = random.Random()


def seeded_rng(seed: object = None) -> random.Random:
    """Build a fresh ``random.Random`` from ``seed`` - inject it into every call.

    Tests pass a fixed int (``seeded_rng(42)``) so the epsilon-greedy draws are
    reproducible; production callers may keep one rng per agent run.
    """
    return random.Random(seed)


def fingerprint_hash(fingerprint: Mapping) -> str:
    """Stable short hash of the fingerprint SHAPE (the PLAN §4.2 transfer key).

    Canonical form: the first 8 hex chars of ``sha256`` over a sorted,
    lowercased ``key=value`` string of the episode fingerprint keys
    (``cdn``/``cms``/``auth``), pipe-joined. Two different AMCs that probe the
    same CDN/CMS/auth combo therefore hash identically - that equality is what
    lets an unmapped AMC inherit a peer's strategy (AC-4) - and any differing
    component changes the hash. Deliberately NOT Python's salted ``hash()``,
    which differs between processes. Missing/``None`` components normalize to
    the empty string, so ``{}`` and ``{"cdn": None}`` hash the same empty shape.
    """
    values: dict[str, str] = {}
    if isinstance(fingerprint, Mapping):
        for key in FINGERPRINT_KEYS:
            value = fingerprint.get(key)
            values[key] = "" if value is None else str(value).strip().lower()
    canonical = "|".join(f"{key}={values.get(key, '')}" for key in sorted(FINGERPRINT_KEYS))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_HASH_CHARS]


def q_update(q: float, reward: float, alpha: float = ALPHA) -> float:
    """SPEC §5 step 6: ``Q_{t+1} = (1 - alpha) * Q_t + alpha * R``.

    Rounded to 2 decimals (the register's stable precision) and floored at 0.0
    so a punishing reward can never drive confidence negative. ``alpha`` must
    lie in ``(0, 1]`` (0.2 per SPEC §5 / PLAN §4.2).
    """
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0.0 < float(alpha) <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    value = (1.0 - float(alpha)) * float(q) + float(alpha) * float(reward)
    return max(CONFIDENCE_FLOOR, round(value, 2))


def _valid_strategy(value: object) -> str | None:
    """Non-empty, non-``manual`` strategy string, stripped; ``None`` otherwise."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() == MANUAL_STRATEGY:
        return None
    return text


def _score_of(entry: dict) -> float:
    score = entry.get("confidence_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0.0
    return float(score)


def _observations_of(entry: dict) -> int:
    observations = entry.get("observations")
    if isinstance(observations, bool) or not isinstance(observations, (int, float)):
        return 0
    return int(observations)


def _sort_key(key: str) -> tuple:
    if key.isdigit():
        return (0, int(key), "")
    return (1, 0, key)


def _is_trusted(entry: dict) -> bool:
    """AC-3 greedy gate: Q >= 5.0, observed at least once, not decayed to the floor."""
    if knowledge.is_stale(entry):
        return False
    if _score_of(entry) < GREEDY_Q_THRESHOLD:
        return False
    return _observations_of(entry) > 0


def _matching_playbooks(register: Mapping, fp_hash: str) -> list[tuple[str, dict, str]]:
    """Playbooks sharing the fingerprint shape, best-scored first, selectable only.

    Entries whose ``best_strategy`` is ``None``/absent/empty/``"manual"`` are
    dropped entirely: they can neither be selected nor offer exploration
    alternatives. Ties break by playbook key (numeric ``mf_id`` keys first,
    then non-numeric source keys) so the order is fully deterministic.
    """
    playbooks = register.get("playbooks") if isinstance(register, Mapping) else None
    if not isinstance(playbooks, dict):
        return []
    matches: list[tuple[str, dict, str]] = []
    for key, entry in playbooks.items():
        if not isinstance(entry, dict) or entry.get("fingerprint_hash") != fp_hash:
            continue
        strategy = _valid_strategy(entry.get("best_strategy"))
        if strategy is None:
            continue
        matches.append((str(key), entry, strategy))
    matches.sort(key=lambda item: (-_score_of(item[1]), _sort_key(item[0])))
    return matches


def _exploration_candidates(matches: list[tuple[str, dict, str]], exclude: str) -> list[str]:
    """Alternative strategies for the fingerprint: peer-proven first, then ladder rungs."""
    candidates: list[str] = []
    for _, _, strategy in matches:
        if strategy != exclude and strategy not in candidates:
            candidates.append(strategy)
    for strategy in STRATEGY_LADDER:
        if strategy != exclude and strategy not in candidates:
            candidates.append(strategy)
    return candidates


def select_strategy(
    register: Mapping,
    fingerprint: Mapping,
    *,
    rng: random.Random | None = None,
) -> str:
    """Epsilon-greedy strategy choice for one fingerprint (SPEC §5 step 2 PLAN).

    See the module docstring for the full greedy / explore / exploit / transfer
    procedure. The register is only read - never mutated. ``rng`` must be a
    ``random.Random`` (``bandit.seeded_rng``) for reproducible draws; ``None``
    falls back to the module default, which is a convenience only.
    """
    rng = rng if rng is not None else _DEFAULT_RNG
    matches = _matching_playbooks(register, fingerprint_hash(fingerprint))
    for _, entry, strategy in matches:
        if _is_trusted(entry):
            return strategy
    if matches:
        best_strategy = matches[0][2]
        if rng.random() < EPSILON:
            candidates = _exploration_candidates(matches, best_strategy)
            if candidates:
                return rng.choice(candidates)
        return best_strategy
    return DEFAULT_STRATEGY


def _ledger(entry: dict) -> dict:
    """The promotion ledger inside the playbook's free-form ``parameters`` object."""
    parameters = entry.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
        entry["parameters"] = parameters
    scores = parameters.get(LEDGER_KEY)
    if not isinstance(scores, dict):
        scores = {}
        parameters[LEDGER_KEY] = scores
    return scores


def _coerce_stats(stats: dict) -> tuple[float, int]:
    q = stats.get("q")
    if isinstance(q, bool) or not isinstance(q, (int, float)):
        q = 0.0
    observations = stats.get("observations")
    if isinstance(observations, bool) or not isinstance(observations, (int, float)):
        observations = 0
    return float(q), int(observations)


def _ledger_stats(entry: dict, strategy: str) -> dict:
    """Writable ledger entry for ``strategy``, created and normalized on demand."""
    scores = _ledger(entry)
    stats = scores.get(strategy)
    if not isinstance(stats, dict):
        stats = {}
        scores[strategy] = stats
    q, observations = _coerce_stats(stats)
    stats["q"] = q
    stats["observations"] = observations
    return stats


def _ledger_view(playbook: dict, strategy: str) -> dict | None:
    """Read-only ledger lookup - never creates entries, never mutates the playbook."""
    parameters = playbook.get("parameters")
    if not isinstance(parameters, dict):
        return None
    scores = parameters.get(LEDGER_KEY)
    if not isinstance(scores, dict):
        return None
    stats = scores.get(strategy)
    if not isinstance(stats, dict):
        return None
    q, observations = _coerce_stats(stats)
    return {"q": q, "observations": observations}


def update_from_episode(register: dict, episode: Mapping, *, now: datetime | str | None = None) -> dict:
    """Fold one finished episode into the register (SPEC §5 step 6); returns the playbook.

    The episode is validated against the §4.1/§7 schema with
    ``episodes.normalize_episode`` first, then the canonical fold is delegated
    to ``knowledge.update_playbook`` - its guards are authoritative: the reward
    only moves ``confidence_score`` when the episode's strategy matches the
    playbook's ``best_strategy`` (a new entry seeds it), and a non-SUCCESS
    outcome may only lower Q. On top of the canonical fold this wrapper
    maintains the per-strategy promotion ledger (``parameters.strategy_scores``):
    the episode's strategy gets a :func:`q_update` blend (mirroring the
    only-lower guard for non-SUCCESS outcomes) plus one observation, and when
    the episode's strategy IS the playbook's ``best_strategy`` the ledger ``q``
    is synced to the register's canonical ``confidence_score`` so the two never
    drift. Promotion itself is a separate, explicit :func:`maybe_promote`
    decision. The register version is bumped (patch) by the knowledge fold.
    """
    if not isinstance(register, dict):
        raise TypeError("register must be a dict")
    record = normalize_episode(episode, now=now)
    mf_id = str(record["mf_id"])
    strategy = str(record["strategy_applied"])
    reward = float(record["reward"])
    outcome = str(record["outcome"])
    entry = knowledge.update_playbook(
        register,
        mf_id,
        strategy=strategy,
        reward=reward,
        outcome=outcome,
        now=now,
        amc_name=str(record["amc_name"]),
        fingerprint_hash=fingerprint_hash(record["fingerprint"]),
    )
    stats = _ledger_stats(entry, strategy)
    new_q = q_update(stats["q"], reward)
    if outcome != "SUCCESS":
        new_q = min(new_q, stats["q"])
    stats["q"] = new_q
    stats["observations"] = int(stats["observations"]) + 1
    if strategy == entry.get("best_strategy"):
        stats["q"] = float(entry["confidence_score"])
    return entry


def maybe_promote(
    playbook: dict,
    candidate_strategy: object,
    *,
    min_observations: int = MIN_PROMOTION_OBSERVATIONS,
    min_q: float = GREEDY_Q_THRESHOLD,
) -> bool:
    """Promote ``candidate_strategy`` to ``best_strategy`` only on real evidence.

    The PLAN §4.2 anti-poisoning guard, enforced here because promotion is the
    bandit's explicit decision: the candidate must have ``observations >=
    min_observations`` (3) AND ``q >= min_q`` (5.0) in the playbook's promotion
    ledger. Refuses (returns ``False`` with no change) when the candidate is
    empty/``"manual"``, already the current ``best_strategy``, or missing from
    the ledger. Never fired by decay: ``knowledge.apply_decay`` neither reads
    nor writes the ledger and never touches ``best_strategy``, so a promotion
    can only ever follow an explicit bandit decision on fresh episode evidence.
    Returns ``True`` when the swap happened.
    """
    if not isinstance(playbook, dict):
        raise TypeError("playbook must be a dict")
    if isinstance(min_observations, bool) or not isinstance(min_observations, int):
        raise TypeError("min_observations must be an int")
    if min_observations < 0:
        raise ValueError("min_observations must be non-negative")
    if isinstance(min_q, bool) or not isinstance(min_q, (int, float)):
        raise TypeError("min_q must be a number")
    strategy = _valid_strategy(candidate_strategy)
    if strategy is None:
        return False
    current = playbook.get("best_strategy")
    if isinstance(current, str) and current.strip() == strategy:
        return False
    stats = _ledger_view(playbook, strategy)
    if stats is None:
        return False
    if stats["observations"] < min_observations:
        return False
    if stats["q"] < min_q:
        return False
    playbook["best_strategy"] = strategy
    return True


__all__ = [
    "DEFAULT_STRATEGY",
    "EPSILON",
    "FINGERPRINT_HASH_CHARS",
    "GAMMA",
    "GREEDY_Q_THRESHOLD",
    "LEDGER_KEY",
    "MANUAL_STRATEGY",
    "MIN_PROMOTION_OBSERVATIONS",
    "STRATEGY_LADDER",
    "fingerprint_hash",
    "maybe_promote",
    "q_update",
    "seeded_rng",
    "select_strategy",
    "update_from_episode",
]
