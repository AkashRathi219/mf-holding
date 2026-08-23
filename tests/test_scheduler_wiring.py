"""Wiring contract between MonthlyScheduler and every callable passed to it.

The P0 bug of 23-Aug-2026 (scheduler called nav_refresh_fn(days=...) against a
zero-arg _nav_job, swallowed by except-Exception) is exactly the class this
test kills permanently: any future signature drift on either side fails CI.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# What MonthlyScheduler actually calls, per src/scheduler.py [S2c/S2d]:
#   pipeline_fn(year=..., month=...)   sync or async both supported
#   nav_refresh_fn(days=int)
#   stock_refresh_fn()
#   bond_refresh_fn()
#   amfi_fn()
CONTRACTS: dict[str, dict] = {
    "pipeline_fn": {"kwargs": ("year", "month"), "sync_ok": True},
    "nav_refresh_fn": {"kwargs": ("days",), "sync_ok": True},
    "stock_refresh_fn": {"kwargs": (), "sync_ok": True},
    "bond_refresh_fn": {"kwargs": (), "sync_ok": True},
    "amfi_fn": {"kwargs": (), "sync_ok": True},
}

WIRING_SITES = [
    ROOT / "webapp" / "main.py",   # in-process scheduler (_start_scheduler_thread)
    ROOT / "main.py",              # workstation CLI scheduler (schedule_start)
]


def _extract_wirings(path: Path) -> list[dict[str, str]]:
    """Find MonthlyScheduler(...) calls; return {kwarg_name: source_expr}."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if fname != "MonthlyScheduler":
            continue
        wiring = {}
        if node.args:  # first positional = pipeline_fn at both call sites
            seg = ast.get_source_segment(src, node.args[0])
            if seg:
                wiring["pipeline_fn"] = seg.strip()
        for kw in node.keywords:
            seg = ast.get_source_segment(src, kw.value)
            if kw.arg and seg:
                wiring[kw.arg] = seg.strip()
        out.append(wiring)
    return out


def _module_for(path: Path):
    import sys
    sys.path.insert(0, str(ROOT))
    if path.name == "main.py" and path.parent == ROOT:
        import main as mod
        return mod
    if path.parent.name == "webapp":
        import webapp.main as mod
        return mod
    raise AssertionError(f"no importer for {path}")


class _StaticFn:
    """inspect.signature-compatible view over an AST FunctionDef, so wiring in
    the CLI entrypoint (root main.py — heavy imports) can be checked without
    executing that module."""

    def __init__(self, name: str, node: ast.AST):
        args = node.args
        names = [a.arg for a in list(args.posonlyargs) + list(args.args)]
        if args.vararg:
            names.append(args.vararg.arg)
        names += [a.arg for a in args.kwonlyargs]
        if args.kwarg:
            names.append(args.kwarg.arg)
        self.parameters = {}
        for n in names:
            kind = (inspect.Parameter.VAR_KEYWORD
                    if args.kwarg and n == args.kwarg.arg
                    else inspect.Parameter.VAR_POSITIONAL
                    if args.vararg and n == args.vararg.arg
                    else inspect.Parameter.POSITIONAL_OR_KEYWORD)
            self.parameters[n] = inspect.Parameter(n, kind)

    def __str__(self):
        return f"<static fn ({', '.join(self.parameters)})>"


def _static_resolver(path: Path):
    """Resolve a source expression to a callable-ish without importing the
    host module: local defs -> _StaticFn; imported symbols -> real getattr on
    the (cheap, stdlib-safe) source module."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    defs: dict[str, ast.AST] = {}
    imports: dict[str, tuple[str | None, str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
        elif isinstance(node, ast.ImportFrom):
            for al in node.names:
                imports[al.asname or al.name] = (node.module, al.name)
        elif isinstance(node, ast.Import):
            for al in node.names:
                imports[al.asname or al.name.split(".")[0]] = (None, al.name)

    def resolve(expr: str):
        expr = expr.strip()
        if re.fullmatch(r"[A-Za-z_]\w*", expr):
            if expr in defs:
                return _StaticFn(expr, defs[expr])
            if expr in imports:
                import importlib
                mod_name, attr = imports[expr]
                target = attr if mod_name is None else f"{mod_name}.{attr}"
                top = target.split(".")[0]
                assert top in ("src", "webapp") or True
                obj = importlib.import_module(mod_name or attr)
                return obj if mod_name is None else getattr(obj, attr)
        pytest.fail(f"cannot statically resolve '{expr}' in {path.name}")

    return resolve


def _params_of(fn):
    """Uniform view: _StaticFn carries .parameters; real callables go through
    inspect.signature."""
    if hasattr(fn, "parameters"):
        return fn.parameters
    return inspect.signature(fn).parameters


@pytest.mark.parametrize("path", WIRING_SITES, ids=lambda p: str(p.relative_to(ROOT)))
def test_wiring_signatures_match_contract(path):
    wirings = _extract_wirings(path)
    assert wirings, f"No MonthlyScheduler wiring found in {path}"
    resolve = _static_resolver(path)
    for wiring in wirings:
        for param, contract in CONTRACTS.items():
            expr = wiring.get(param)
            assert expr, f"{path}: missing '{param}' in wiring {wiring}"
            fn = resolve(expr)
            params = _params_of(fn)
            has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in params.values())
            for kw in contract["kwargs"]:
                assert kw in params or has_var_kw, (
                    f"{path}: {expr}({', '.join(params)}) does not accept "
                    f"'{kw}' (scheduler passes {contract['kwargs']})")


def test_nav_job_defaults_to_7_days():
    from webapp.main import _nav_job
    sig = inspect.signature(_nav_job)
    assert "days" in sig.parameters
    assert sig.parameters["days"].default == 7


def test_nav_job_passes_days_through():
    src = (ROOT / "webapp" / "main.py").read_text(encoding="utf-8")
    assert "_update_latest_navs_impl(days=days)" in src


def test_amfi_job_guarded():  # [S2e]
    src = (ROOT / "webapp" / "main.py").read_text(encoding="utf-8")
    assert "from webapp.amfi_fetch import fetch_mfdata, save" in src
    assert "import failed" in src  # guard message inside _amfi_job


def test_startup_resilient(monkeypatch):  # [S2b]
    """ENABLE_SCHEDULER=1 with broken deps must degrade quietly, never raise
    out of the lifespan hook (the crash-loop class of 23-Aug-2026)."""
    import builtins

    from webapp import main

    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name.split(".")[0] in ("yaml", "apscheduler"):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *a, **k)

    monkeypatch.setenv("ENABLE_SCHEDULER", "1")
    monkeypatch.setattr(builtins, "__import__", boom)
    monkeypatch.setattr(main, "_job_runner_started", False)

    main._on_startup()  # must not raise
    assert main._job_runner_started is False  # degraded: no thread spawned


def test_pipeline_fn_sync_ok(tmp_path):  # [S2d] sync pipeline_fn no longer TypeError
    import asyncio
    from datetime import datetime
    from src.scheduler import MonthlyScheduler

    got: list[tuple] = []

    def sync_pipeline(**kwargs):
        got.append(tuple(sorted(kwargs)))

    sched = MonthlyScheduler(sync_pipeline, {"scheduler": {"enabled": True}},
                             base_dir=tmp_path,
                             stock_refresh_fn=lambda: None)
    now = datetime.now()
    asyncio.run(sched._run_pipeline())
    assert got == [("month", "year")]
    assert (tmp_path / "logs" / f"success_{now.year}-{now.month:02d}.marker").exists()


class _Job:
    def __init__(self, id, name, nxt):  # noqa: A002
        self.id, self.name, self.next_run_time = id, name, nxt


def test_heartbeat_recorded(tmp_path, monkeypatch):  # [S2f]
    import src.refresh_log as rl
    from src.scheduler import MonthlyScheduler

    monkeypatch.setattr(rl, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rl, "LOG_PATH", tmp_path / "log.jsonl")
    monkeypatch.setattr(rl, "_state_cache", None)

    sched = MonthlyScheduler(lambda **k: None, {}, base_dir=tmp_path)
    sched.scheduler = type("FakeSched", (), {
        "get_jobs": staticmethod(lambda: [_Job("daily_nav_refresh", "Daily NAV",
                                               None)])})()
    sched._record_heartbeat()

    state = rl.read_state()
    entry = state["pipelines"]["scheduler"]
    assert entry["last_status"] == "alive"
    assert entry["last_detail"]["jobs"][0]["id"] == "daily_nav_refresh"
