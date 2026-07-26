"""Every test that spawns the imp CLI as a subprocess must pass an explicit
`cwd`. Without one the child runs at the pytest invocation dir — the maintainer's
own checkout — where `imp.__main__:main` would stage a `git mv` of a tracked file
mid-suite. That is a working-tree mutation no assertion would otherwise catch."""
from __future__ import annotations

import ast
from pathlib import Path

# TerminalSession is impty's pty driver: it spawns the same argv through a pty
# and reaches the same entrypoint, so it is a spawner for this guard's purposes.
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output",
             "TerminalSession"}
# argv[0] spellings of the imp binary across the suite: the `write_imp_shim`
# product and the `--imp` path threaded into check_dogfood_invariants.py.
_IMP_BIN = {"shim", "imp", "imp_bin", "str(shim)", "str(imp)", "str(imp_bin)"}


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _spawns_imp_cli(call: ast.Call) -> bool:
    if _callee(call) not in _SPAWNERS:
        return False
    if not call.args or not isinstance(call.args[0], ast.List):
        return False
    elts = call.args[0].elts
    consts = [e.value if isinstance(e, ast.Constant) else None for e in elts]
    if any(consts[i] == "-m" and consts[i + 1] == "imp" for i in range(len(consts) - 1)):
        return True
    return bool(elts) and ast.unparse(elts[0]) in _IMP_BIN


def test_every_imp_cli_spawn_passes_an_explicit_cwd() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    spawns = 0
    for path in sorted((root / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _spawns_imp_cli(node)):
                continue
            spawns += 1
            if not any(kw.arg == "cwd" for kw in node.keywords):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
    # The walker itself must not silently stop matching (a renamed helper, a
    # reshaped argv) and pass vacuously.
    assert spawns >= 22, f"only {spawns} imp CLI spawns found — the matcher drifted"
