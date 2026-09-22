"""AC 10a: disk state reaches no path that had a witness available. The closing
block's one surviving read and the startup-failure check are the only callers of
resolve_settled_outcome in src/ — a third would be a path rendering a state it
never observed."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "imp"

# (module path relative to src/imp, enclosing function or "<module>") — never line numbers.
_ALLOWED = frozenset({
    ("cli/session_run.py", "_closing_block"),
    ("cli/session_run.py", "run_session_client"),
})

_MODULE_SCOPE = "<module>"


def _is_target_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name)
                  and node.func.id == "resolve_settled_outcome")
                 or (isinstance(node.func, ast.Attribute)
                     and node.func.attr == "resolve_settled_outcome")))


def _visit(node: ast.AST, enclosing: str, module: str, sites: set[tuple[str, str]]) -> None:
    for child in ast.iter_child_nodes(node):
        if _is_target_call(child):
            sites.add((module, enclosing))
        inner = (child.name
                 if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                 else enclosing)
        _visit(child, inner, module, sites)


def _call_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        _visit(tree, _MODULE_SCOPE, path.relative_to(_SRC).as_posix(), sites)
    return sites


def test_resolve_settled_outcome_has_exactly_two_callers_in_src() -> None:
    assert _call_sites() == _ALLOWED
