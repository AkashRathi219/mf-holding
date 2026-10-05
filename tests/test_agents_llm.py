"""T9: LLM extraction fallback (AC-9) - opencode CLI first, OpenRouter GLM
second, empty-body short-circuit, per-cycle budget guard. Every test drives
extract() through an injected recording runner: zero network, zero
subprocess, zero API keys. The only real transport code exercised offline is
the OpenRouter no-key path, which reports unavailable before any HTTP call.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from src.agents import llm
from src.agents.llm import (
    DEFAULT_FREE_MODEL,
    EMPTY_BODY_BUDGET_SECONDS,
    FALLBACK_MODEL,
    CycleBudget,
    LlmBudgetExceeded,
    extract,
)
from src.agents.taxonomy import is_valid_code

ROOT = Path(__file__).resolve().parents[1]


class RecordingRunner:
    """Fake transport runner: records call order (backend, prompt, model,
    timeout) and returns the scripted body; an Exception value in the script
    is raised instead."""

    def __init__(self, script: dict):
        self.script = dict(script)
        self.calls: list[tuple[str, str, str, float]] = []

    def __call__(self, backend, prompt, *, model, timeout):
        self.calls.append((backend, prompt, model, timeout))
        body = self.script[backend]
        if isinstance(body, Exception):
            raise body
        return body


def _configured_key_env() -> str:
    from src import ai_extract

    return str(ai_extract.load_cfg().get("api_key_env") or "OPENROUTER_API_KEY")


def test_opencode_is_attempted_before_openrouter():
    runner = RecordingRunner({"opencode": '{"schemes": []}',
                              "openrouter": "OPENROUTER_MUST_NOT_RUN"})
    result = extract("extract the holdings", runner=runner)
    assert result.ok is True
    assert result.backend == "opencode"
    assert result.model == DEFAULT_FREE_MODEL
    assert [call[0] for call in runner.calls] == ["opencode"]
    assert result.attempts[0].backend == "opencode"
    assert len(result.attempts) == 1

    failing = RecordingRunner({"opencode": RuntimeError("cli exploded"),
                               "openrouter": '{"schemes": []}'})
    result2 = extract("extract the holdings", runner=failing)
    assert [call[0] for call in failing.calls] == ["opencode", "openrouter"]
    assert result2.backend == "openrouter"


def test_empty_opencode_body_falls_through_to_openrouter():
    runner = RecordingRunner({"opencode": "   \n\t", "openrouter": '{"schemes": []}'})
    result = extract("extract the holdings", runner=runner)
    assert result.ok is True
    assert result.backend == "openrouter"
    assert result.model == FALLBACK_MODEL
    assert [call[0] for call in runner.calls] == ["opencode", "openrouter"]
    assert result.attempts[0].empty is True
    assert result.attempts[0].ok is False
    assert result.attempts[0].failure_code == "ERR_LLM_EMPTY_REASONING"
    assert result.attempts[1].ok is True


def test_both_transports_empty_short_circuits_within_budget():
    runner = RecordingRunner({"opencode": "", "openrouter": "  "})
    result = extract("extract the holdings", runner=runner)
    assert result.ok is False
    assert result.text == ""
    assert [call[0] for call in runner.calls] == ["opencode", "openrouter"]
    assert len(result.attempts) == 2
    assert result.failure_code == "ERR_LLM_EMPTY_REASONING"
    assert is_valid_code(result.failure_code)
    assert result.elapsed_seconds <= EMPTY_BODY_BUDGET_SECONDS


def test_per_cycle_budget_guard_stops_after_max_calls():
    runner = RecordingRunner({"opencode": "", "openrouter": ""})
    result = extract("p", runner=runner, max_calls=1)
    assert result.ok is False
    assert len(runner.calls) == 1
    assert len(result.attempts) == 1
    assert result.attempts[0].backend == "opencode"

    budget = CycleBudget(max_calls=3)
    assert extract("p1", runner=runner, budget=budget).ok is False
    assert extract("p2", runner=runner, budget=budget).ok is False
    assert len(runner.calls) == 4
    assert budget.used == 3
    assert budget.remaining == 0
    refused = extract("p3", runner=runner, budget=budget)
    assert refused.ok is False
    assert refused.attempts == ()
    assert len(runner.calls) == 4
    assert "budget" in (refused.error or "").lower()
    with pytest.raises(LlmBudgetExceeded):
        budget.consume()
        budget.consume()


def test_openrouter_without_key_reports_unavailable_cleanly(monkeypatch):
    env_var = _configured_key_env()
    monkeypatch.delenv(env_var, raising=False)

    def openrouter_only(backend, prompt, *, model, timeout):
        assert backend == "openrouter"
        return llm._openrouter_transport(prompt, model=model, timeout=timeout)

    result = extract("extract the holdings",
                     backend_preference=("openrouter",), runner=openrouter_only)
    assert result.ok is False
    assert result.text == ""
    assert "unavailable" in (result.error or "").lower()
    assert env_var in (result.error or "")
    assert result.attempts[0].ok is False
    assert result.failure_code is None or is_valid_code(result.failure_code)


def test_api_key_never_leaks_into_produced_objects(monkeypatch):
    env_var = _configured_key_env()
    fake_key = "sk-fake-secret-abcdef1234567890"
    monkeypatch.setenv(env_var, fake_key)

    runner = RecordingRunner({"opencode": "", "openrouter": '{"schemes": []}'})
    result = extract("extract the holdings", runner=runner)

    rendered = "\n".join([
        repr(result),
        str(result),
        str(result.attempts),
        json.dumps(asdict(result), default=str),
    ])
    assert fake_key not in rendered
    for call in runner.calls:
        assert fake_key not in repr(call)

    budget = CycleBudget(max_calls=1)
    with pytest.raises(LlmBudgetExceeded) as ei:
        budget.consume()
        budget.consume()
    assert fake_key not in repr(ei.value)


_FRESH_IMPORT_CHECK = (
    "import json, sys; "
    "import src.agents.llm as m; "
    "print(json.dumps({"
    "'imported': 'src.agents.llm' in sys.modules, "
    "'banned': [name for name in ('httpx', 'playwright') if name in sys.modules], "
    "'free_model': m.DEFAULT_FREE_MODEL, "
    "'fallback_model': m.FALLBACK_MODEL}))"
)


def test_fresh_import_pulls_no_httpx_or_playwright():
    proc = subprocess.run([sys.executable, "-c", _FRESH_IMPORT_CHECK],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["imported"] is True
    assert payload["banned"] == []
    assert payload["free_model"] == "opencode/space-bunny-free"
    assert payload["fallback_model"] == "z-ai/glm-5.3-flash"


def test_llm_module_source_has_no_heavy_top_level_imports():
    heavy = {"httpx", "playwright", "pandas", "pymupdf", "fitz", "webapp"}
    tree = ast.parse(Path(llm.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = [node.module or ""]
        else:
            continue
        for target in targets:
            assert target.split(".")[0] not in heavy, target


def test_success_returns_payload_from_whichever_transport_succeeded():
    payload = ('{"schemes": [{"name": "Test Fund", "date": "2026-09-30", '
               '"holdings": [{"company": "Test Industries Ltd", '
               '"percent_nav": 4.2, "isin": null}]}]}')
    runner = RecordingRunner({"opencode": payload, "openrouter": "UNUSED"})
    result = extract("extract the holdings", runner=runner)
    assert result.ok is True
    assert result.text == payload
    assert result.backend == "opencode"
    assert result.model == DEFAULT_FREE_MODEL
    assert result.failure_code is None
    assert result.error is None
    assert result.attempts[-1].ok is True

    runner2 = RecordingRunner({"opencode": RuntimeError("cli not usable"),
                               "openrouter": payload})
    result2 = extract("extract the holdings", runner=runner2)
    assert result2.ok is True
    assert result2.text == payload
    assert result2.backend == "openrouter"
    assert result2.model == FALLBACK_MODEL
    assert [call[0] for call in runner2.calls] == ["opencode", "openrouter"]
    assert result2.attempts[0].ok is False
    assert result2.attempts[0].failure_code is None
