"""Slim-dependency guard: nothing reachable from webapp.main may import a
third-party package outside deploy/requirements-slim.txt — at module scope OR
inside functions on the ENABLE_SCHEDULER=1 path — unless the import is guarded
by try/except ImportError-style handling.

Permanent fix for the 23-Aug-2026 deploy-brick class (pyyaml / apscheduler
imported unguarded at startup while absent from the Railway image).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# import-name -> present in deploy/requirements-slim.txt (+ stdlib assumed).
ALLOWED = {
    "fastapi", "uvicorn", "pydantic", "boto3", "botocore", "multipart",
    "pdfplumber",
    "yaml",          # pyyaml        [S2a]
    "apscheduler",   #               [S2a]
    "httpx",         #               [S2a]
    "curl_cffi",     # NSE APIs Chrome impersonation [PLAN_STOCK_DATA_NSE_CLEANUP]
}
LOCAL_PACKAGES = {"webapp", "src"}
GUARDING_EXC = {"Exception", "BaseException", "ImportError", "ModuleNotFoundError"}

# Justified exceptions: imports inside CLI-only entry points never executed by
# the server process. Keep short; every entry needs an argument.
PER_FILE_ALLOW: dict[str, set[str]] = {
    "webapp/amfi_fetch.py": {"click"},  # its __main__ CLI block only
    "src/stock_identity.py": {"click"},  # its __main__ CLI block only
}


def _build_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _guarded(stmt: ast.stmt, parents: dict[ast.AST, ast.AST]) -> bool:
    cur = stmt
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.Try):
            for handler in cur.handlers:
                exc = handler.type
                names: list[str] = []
                if isinstance(exc, ast.Name):
                    names = [exc.id]
                elif isinstance(exc, ast.Attribute):
                    names = [exc.attr]
                elif isinstance(exc, ast.Tuple):
                    names = [e.id for e in exc.elts if isinstance(e, ast.Name)]
                elif exc is None:
                    names = ["Exception"]  # bare except
                if any(n.split(".")[-1] in GUARDING_EXC for n in names):
                    return True
    return False


def _module_file(full: str) -> Path | None:
    rel = full.replace(".", "/")
    for cand in (ROOT / f"{rel}.py", ROOT / rel / "__init__.py"):
        if cand.exists():
            return cand
    return None


def collect_violations(entry: str = "webapp.main") -> list[str]:
    """Walk EVERY import statement reachable from `entry` (function bodies
    count: the scheduler runs inside functions) and flag unguarded imports of
    third-party tops outside ALLOWED."""
    violations: list[str] = []
    seen_files: set[Path] = set()
    queue: list[tuple[str, str]] = [(entry, entry)]  # (module, reached-via)
    chain: dict[str, str] = {}
    while queue:
        modname, via = queue.pop()
        path = _module_file(modname)
        if path is None or path in seen_files:
            continue
        seen_files.add(path)
        chain[modname] = via
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        parents = _build_parents(tree)
        rel_label = path.relative_to(ROOT).as_posix()
        allow = PER_FILE_ALLOW.get(rel_label, set())
        for stmt in ast.walk(tree):
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            targets: list[tuple[str, str | None]] = []  # (top, local_target)
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    targets.append((alias.name.split(".")[0], alias.name))
            else:
                if stmt.level > 0:
                    base = ".".join(modname.split(".")[:-1]) or modname
                    target = ".".join(filter(None, [base, stmt.module]))
                else:
                    target = stmt.module
                top = (target or "").split(".")[0]
                targets.append((top, target))
            for top, target in targets:
                if not top:
                    continue
                if top in LOCAL_PACKAGES:
                    if target and target not in chain:
                        queue.append((target, modname))
                    continue
                if top in sys.stdlib_module_names or top in ALLOWED:
                    continue
                if _guarded(stmt, parents):
                    continue
                if top in allow:
                    continue
                trail = " -> ".join([modname])
                back, cur = [], modname
                while cur in chain and chain[cur] != cur:
                    back.append(chain[cur])
                    cur = chain[cur]
                trail = " -> ".join(list(reversed(back)) + [modname])
                violations.append(
                    f"{rel_label}:{stmt.lineno} imports '{top}' "
                    f"[via {trail}]")
    return violations


def test_boot_graph_fully_covered_by_slim_requirements():
    violations = collect_violations("webapp.main")
    assert not violations, (
        "Boot graph has imports outside requirements-slim:\n"
        + "\n".join(violations))


def test_allowlist_matches_requirements_slim_file():
    slim = (ROOT / "deploy" / "requirements-slim.txt").read_text(encoding="utf-8")
    dists = {ln.split(">=")[0].split("<")[0].split("=")[0].strip().lower()
             for ln in slim.splitlines()
             if ln.strip() and not ln.strip().startswith("#")}
    dist_to_import = {"pyyaml": "yaml", "python-multipart": "multipart"}
    import_names = {dist_to_import.get(d, d) for d in dists}
    missing = ALLOWED - import_names
    assert not missing, f"ALLOWED entries absent from requirements-slim: {missing}"


def test_guard_pattern_is_tolerated(tmp_path):
    """Regression guard for the walker itself: a try/except ImportError around
    a non-slim import must NOT be flagged."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "try:\n    import pandas\nexcept ImportError:\n    pandas = None\n",
        encoding="utf-8")
    tree = ast.parse(probe.read_text(encoding="utf-8"))
    stmt = next(s for s in ast.walk(tree)
                if isinstance(s, (ast.Import, ast.ImportFrom)))
    assert _guarded(stmt, _build_parents(tree)) is True
