"""Autonomous AMC scraping agents (SPEC: amc-holdings-agents).

Importing this package must stay cheap: only the fixed failure taxonomy is
loaded here. Heavier agent modules (runner, bandit, integrity, sources, ...)
are imported directly by their callers, never re-exported from this init.
"""

from __future__ import annotations

from src.agents.taxonomy import (
    FAILURE_CODES,
    FAILURE_METADATA,
    FailureCodeInfo,
    classify_exception,
    is_valid_code,
)

__all__ = [
    "FAILURE_CODES",
    "FAILURE_METADATA",
    "FailureCodeInfo",
    "classify_exception",
    "is_valid_code",
]
