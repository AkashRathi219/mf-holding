"""LLM extraction fallback orchestrator (SPEC OQ-2, AC-9, PLAN T9).

Thin, testable orchestrator over the two transports that already work in
production. The opencode CLI invocation mirrors scripts/linksheet_sync.py
(``shutil_which_opencode`` + ``_oc_run``): the npm .cmd/.ps1 shim found on
PATH garbles multi-line arguments, so it is mapped to the sibling
``node_modules/opencode-ai/bin/opencode.exe``; the prompt is passed as the
positional message (the CLI's ``-f`` attachment flag greedily swallows it and
must not be used). The OpenRouter side reuses the src/ai_extract.py plumbing
(``load_cfg``, ``SYSTEM_PROMPT``, ``_headers``, ``ExtractError``) and applies
linksheet_sync._ai_chat's 16k ``max_tokens`` cap (GLM 5.3 is a reasoning
model: when its thinking consumes the whole budget the response arrives with
EMPTY content).

Transport order (OQ-2): the free ``opencode/space-bunny-free`` CLI route is
attempted FIRST; the billed OpenRouter route (``z-ai/glm-5.3-flash``) runs
only on CLI failure.

AC-9 empty-response short-circuit: an empty/whitespace body is recorded as
``ERR_LLM_EMPTY_REASONING`` - classified via ``taxonomy.classify_exception``
on the same ``ai_extract.ExtractError`` the production path raises for empty
model content - and the orchestrator moves on immediately: no same-transport
retry, no inter-attempt sleep, no backoff. The fallback decision therefore
costs no measurable wall-clock: the dispatch overhead accrued OUTSIDE
transport I/O must stay under ``EMPTY_BODY_BUDGET_SECONDS`` (5.0) or the
cycle stops rather than dithers. An OpenRouter timeout raises the same
ExtractError family (mirroring ``_ai_chat``), so a timed-out GLM call also
falls through to the CLI within the same decision budget; the opencode CLI
timeout surfaces as an empty/partial body and takes the same path.

Cost control: a per-cycle call budget (``CycleBudget`` / ``max_calls``) caps
how many LLM calls one cycle may spend; once exhausted, further attempts are
refused as a typed failure (``LlmBudgetExceeded``), never a silent retry
loop. ``extract`` never returns ``None`` and never raises for transport or
budget problems - it always returns an ``LlmResult`` whose ``failure_code``
is a taxonomy code, or ``None`` when the taxonomy (by design) has no home for
that failure family (e.g. missing credentials); codes are never invented.

Dependency injection: pass ``runner`` to substitute both transports with a
recording fake - tests never shell out to opencode, never call OpenRouter
and perform zero network I/O. The runner contract is
``runner(backend, prompt, *, model, timeout) -> str`` where ``backend`` is
``"opencode"`` or ``"openrouter"``. Without injection ``_default_runner``
dispatches to the real transports.

Secrets: the OpenRouter key is read ONLY from the env var named by
``ai_extract.load_cfg()`` (``api_key_env``, default ``OPENROUTER_API_KEY``)
and only inside the OpenRouter transport (``ai_extract._headers`` attaches it
to the request). It is never stored, logged, echoed or accepted as a literal;
with the key absent the transport raises ``TransportUnavailable`` and the
cycle degrades to a clean failure.

Import-light: module import pulls only the stdlib plus the fixed failure
taxonomy. httpx (via src.ai_extract) and subprocess are imported lazily
inside the transport functions, so ``import src.agents.llm`` stays cheap for
every agent module that never reaches the LLM tier.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.agents.taxonomy import classify_exception

logger = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parents[2]

DEFAULT_FREE_MODEL = "opencode/space-bunny-free"
FALLBACK_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_BACKEND_ORDER: tuple[str, ...] = ("opencode", "openrouter")
EMPTY_BODY_BUDGET_SECONDS = 5.0
DEFAULT_MAX_CALLS = 4
DEFAULT_TIMEOUT_SECONDS = 120.0
OPENROUTER_MAX_TOKENS = 16000

_KNOWN_BACKENDS = frozenset({"opencode", "openrouter"})

Runner = Callable[..., str]


class TransportUnavailable(RuntimeError):
    """A transport cannot run at all (CLI missing from PATH, credentials absent)."""


class LlmBudgetExceeded(RuntimeError):
    """The per-cycle LLM call budget is exhausted (cost guard)."""


@dataclass(frozen=True)
class LlmAttempt:
    """One transport attempt. Never carries the prompt or the response body."""

    backend: str
    model: str
    ok: bool
    empty: bool
    failure_code: str | None
    error: str
    seconds: float


@dataclass(frozen=True)
class LlmResult:
    """Outcome of one extract() cycle. Safe to log: no key material, no body."""

    ok: bool
    text: str
    backend: str | None
    model: str | None
    failure_code: str | None
    error: str | None
    attempts: tuple[LlmAttempt, ...]
    elapsed_seconds: float


@dataclass
class CycleBudget:
    """Mutable per-cycle call budget shared by every extract() in one cycle."""

    max_calls: int = DEFAULT_MAX_CALLS
    used: int = 0

    def try_consume(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True

    def consume(self) -> None:
        if not self.try_consume():
            raise LlmBudgetExceeded(
                f"per-cycle LLM budget exhausted: {self.used}/{self.max_calls} calls used")

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)


def _opencode_executable() -> str:
    """Resolve the REAL opencode executable (mirrors
    scripts/linksheet_sync.shutil_which_opencode): the npm .cmd/.ps1 shim on
    PATH cannot be executed by subprocess directly and garbles multi-line
    arguments, so map it to the sibling node_modules/opencode-ai exe."""
    import shutil

    exe = shutil.which("opencode")
    if not exe:
        raise TransportUnavailable("opencode CLI not found on PATH")
    p = Path(exe)
    if p.name.lower().endswith((".cmd", ".ps1")):
        cand = p.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if cand.exists():
            return str(cand)
    return exe


def _opencode_transport(prompt: str, *, model: str, timeout: float) -> str:
    """One free opencode CLI turn (mirrors scripts/linksheet_sync._oc_run):
    the CLI reads referenced files with its own tools and prints the answer to
    stdout; a timeout is killed and surfaces as an empty/partial body."""
    import subprocess

    exe = _opencode_executable()
    cmd = [exe, "run", "-m", model, prompt]
    proc = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
    return (out or "") + "\n" + (err or "")


def _openrouter_transport(prompt: str, *, model: str, timeout: float) -> str:
    """One OpenRouter chat turn. Reuses the src/ai_extract plumbing and the
    linksheet_sync._ai_chat payload shape (temperature 0, JSON mode, 16k
    max_tokens). Single attempt: retry policy belongs to the orchestrator,
    which deliberately does not burn the cycle budget on empties (AC-9)."""
    import os

    from src import ai_extract

    cfg = ai_extract.load_cfg()
    env_var = str(cfg.get("api_key_env") or "OPENROUTER_API_KEY")
    if not os.environ.get(env_var):
        raise TransportUnavailable(
            f"openrouter unavailable: no API key in env var {env_var}")
    import httpx

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": ai_extract.SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": OPENROUTER_MAX_TOKENS,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{cfg['base_url']}/chat/completions",
                            headers=ai_extract._headers(cfg), json=payload)
    except (httpx.TransportError, httpx.TimeoutException) as e:
        raise ai_extract.ExtractError(f"AI provider unreachable: {e}") from e
    if r.status_code >= 500 or r.status_code == 429:
        raise ai_extract.ExtractError(f"AI provider unreachable: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise ai_extract.ExtractError(
            f"provider rejected request: HTTP {r.status_code} {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise ai_extract.ExtractError("openrouter returned a non-JSON body") from e
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return content or ""


def _default_runner(backend: str, prompt: str, *, model: str, timeout: float) -> str:
    if backend == "opencode":
        return _opencode_transport(prompt, model=model, timeout=timeout)
    if backend == "openrouter":
        return _openrouter_transport(prompt, model=model, timeout=timeout)
    raise ValueError(f"unknown LLM transport backend: {backend!r}")


def _empty_body_error(backend: str) -> BaseException:
    """The production failure for an empty completion: ai_extract.ExtractError
    when importable (the taxonomy maps it to ERR_LLM_EMPTY_REASONING), else a
    plain RuntimeError that classifies as unclassified."""
    try:
        from src.ai_extract import ExtractError
    except Exception:
        return RuntimeError(f"{backend} returned an empty response body")
    return ExtractError(
        f"{backend} returned an empty response body (reasoning model likely "
        "exhausted its token budget)")


def extract(
    prompt: str,
    *,
    backend_preference: Sequence[str] = DEFAULT_BACKEND_ORDER,
    free_model: str = DEFAULT_FREE_MODEL,
    fallback_model: str = FALLBACK_MODEL,
    max_calls: int = DEFAULT_MAX_CALLS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner | None = None,
    budget: CycleBudget | None = None,
) -> LlmResult:
    """Run one LLM extraction through the transport ladder (OQ-2 / AC-9).

    Tries each backend in ``backend_preference`` (default: the free opencode
    CLI first, OpenRouter second). The first non-empty body wins and is
    returned verbatim in ``LlmResult.text``. An empty body or a raised
    transport error is recorded as an ``LlmAttempt`` and the NEXT backend
    runs immediately - no same-transport retry, no sleeps - so the fallback
    decision stays inside ``EMPTY_BODY_BUDGET_SECONDS`` of orchestrator
    overhead. Returns a failure ``LlmResult`` (never ``None``, never raises)
    when every backend failed or the per-cycle budget guard stopped the
    cycle; ``failure_code`` comes from ``taxonomy.classify_exception`` and is
    ``None`` only when the taxonomy has no home for that failure family.
    ``ValueError`` is raised for caller programming errors only (empty
    prompt, unknown backend, empty preference).
    """
    text = str(prompt or "")
    if not text.strip():
        raise ValueError("prompt must be a non-empty string")
    run = runner or _default_runner
    models = {"opencode": free_model, "openrouter": fallback_model}
    backends: list[str] = []
    for backend in backend_preference:
        if backend not in _KNOWN_BACKENDS:
            raise ValueError(f"unknown backend in backend_preference: {backend!r}")
        if backend not in backends:
            backends.append(backend)
    if not backends:
        raise ValueError("backend_preference must name at least one transport")
    cycle = budget if budget is not None else CycleBudget(max_calls=max_calls)

    attempts: list[LlmAttempt] = []
    t0 = time.perf_counter()
    runner_seconds = 0.0
    stop_reason = ""

    for backend in backends:
        if not cycle.try_consume():
            stop_reason = (
                "per-cycle budget guard: "
                f"{cycle.used}/{cycle.max_calls} LLM calls used, refusing more")
            break
        if attempts:
            overhead = (time.perf_counter() - t0) - runner_seconds
            if overhead > EMPTY_BODY_BUDGET_SECONDS:
                stop_reason = (
                    "empty-body short-circuit budget exceeded: dispatch "
                    f"overhead {overhead:.3f}s > {EMPTY_BODY_BUDGET_SECONDS:.1f}s")
                break
        model = models[backend]
        t_call = time.perf_counter()
        error = ""
        failure_code: str | None = None
        empty = False
        succeeded = False
        body_out = ""
        try:
            raw = run(backend, text, model=model, timeout=timeout)
        except Exception as exc:
            call_seconds = time.perf_counter() - t_call
            error = f"{type(exc).__name__}: {exc}"
            failure_code = classify_exception(exc)
            logger.warning("LLM transport %s failed: %s", backend, error)
        else:
            call_seconds = time.perf_counter() - t_call
            raw_text = str(raw or "")
            if raw_text.strip():
                succeeded = True
                body_out = raw_text
            else:
                empty = True
                empty_exc = _empty_body_error(backend)
                failure_code = classify_exception(empty_exc)
                error = str(empty_exc)
                logger.warning("LLM transport %s returned an empty body", backend)
        runner_seconds += call_seconds
        attempts.append(LlmAttempt(
            backend=backend, model=model, ok=succeeded, empty=empty,
            failure_code=failure_code, error=error,
            seconds=round(call_seconds, 4)))
        if succeeded:
            elapsed = round(time.perf_counter() - t0, 4)
            logger.info("LLM extract ok via %s (%s) in %.3fs", backend, model, elapsed)
            return LlmResult(
                ok=True, text=body_out, backend=backend, model=model,
                failure_code=None, error=None, attempts=tuple(attempts),
                elapsed_seconds=elapsed)

    last = attempts[-1] if attempts else None
    return LlmResult(
        ok=False, text="", backend=None, model=None,
        failure_code=last.failure_code if last else None,
        error=stop_reason or (last.error if last else "no transport attempted"),
        attempts=tuple(attempts),
        elapsed_seconds=round(time.perf_counter() - t0, 4))


__all__ = [
    "DEFAULT_BACKEND_ORDER",
    "DEFAULT_FREE_MODEL",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_TIMEOUT_SECONDS",
    "EMPTY_BODY_BUDGET_SECONDS",
    "FALLBACK_MODEL",
    "CycleBudget",
    "LlmAttempt",
    "LlmBudgetExceeded",
    "LlmResult",
    "Runner",
    "TransportUnavailable",
    "extract",
]
