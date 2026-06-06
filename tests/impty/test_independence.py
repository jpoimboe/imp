"""impty is a GENERIC pty/pyte terminal harness — it must NOT depend on imp.

The mockup renderer (impty.render) paints whatever app it's pointed at; the
app-specific theme is INJECTED by the imp-aware caller (scripts/snap_imp_ui.py),
not imported. This test enforces that boundary so the dependency can't creep back
in: a static scan asserts no module under src/impty imports the `imp` package,
and a runtime check confirms importing impty never pulls imp into sys.modules.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import impty


def _impty_root() -> Path:
    root = Path(impty.__file__).resolve().parent
    assert (root / "render.py").exists(), f"unexpected impty layout at {root}"
    return root


def _top_level(dotted: str) -> str:
    """First component of a dotted module path. 'imp.cli.palette' -> 'imp';
    'impty.render' -> 'impty' (so the `imp` prefix of `impty` never false-matches)."""
    return dotted.split(".", 1)[0]


def _imported_packages(source: str) -> set[str]:
    """Top-level package names imported by `source` (absolute imports only;
    relative `from . import x` stays within impty and is ignored)."""
    pkgs: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pkgs.update(_top_level(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            pkgs.add(_top_level(node.module))
    return pkgs


def test_no_impty_module_imports_imp() -> None:
    offenders: list[str] = []
    for py in sorted(_impty_root().rglob("*.py")):
        if "imp" in _imported_packages(py.read_text(encoding="utf-8")):
            offenders.append(str(py))
    assert not offenders, (
        "impty must stay independent of imp, but these modules import the `imp` "
        "package:\n  " + "\n  ".join(offenders) + "\n"
        "Inject app-specific values from the imp-aware caller instead "
        "(e.g. scripts/snap_imp_ui.py passes render_screen(palette=, bg=, fg=))."
    )


def test_importing_impty_does_not_pull_in_imp() -> None:
    # Belt-and-suspenders for dynamic/lazy imports the AST scan can't see: import
    # the whole impty package in a fresh interpreter and assert `imp` never loaded.
    code = (
        "import importlib, pkgutil, sys, impty\n"
        "for m in list(pkgutil.walk_packages(impty.__path__, 'impty.')):\n"
        "    importlib.import_module(m.name)\n"
        "bad = sorted(n for n in sys.modules if n == 'imp' or n.startswith('imp.'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    leaked = [n for n in out.stdout.strip().split(",") if n]
    assert not leaked, f"importing impty pulled in imp modules: {leaked}"
