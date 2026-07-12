"""Heuristic detection of a project's import roots for the PYTHONPATH shadow.

Proposes an `imp/project.yaml` `pythonpath` value at `imp init` (config-birth):
worktree-relative directories prepended to gate/agent subprocess PYTHONPATH so an
editable-installed package resolves to the worktree under test, not a stale main
checkout. Suggestions only — the user accepts, edits, or skips.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path


def detect_pythonpath(project_root: Path) -> list[str] | None:
    """Propose import root(s) for `project_root`, or None when it is not a
    Python package imp should shadow.

    - a `src/` directory containing a Python package (`*/__init__.py` under
      `src/`) => `["src"]` (src-layout);
    - else a root `pyproject.toml` (flat-layout package) => `["."]`;
    - else None.
    """
    src = project_root / "src"
    if src.is_dir() and any(src.glob("*/__init__.py")):
        return ["src"]
    if (project_root / "pyproject.toml").is_file():
        return ["."]
    return None


def resolve_pythonpath_choice(
    suggested: list[str],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> list[str] | None:
    """Present a detected pythonpath for accept / edit / skip. Returns the
    accepted (or edited) roots, or None when the user skips."""
    output_fn(
        f"Detected import root(s) {suggested} — prepend to the worktree's "
        "PYTHONPATH so the test gate runs the worktree, not a stale editable "
        "install? [Y / edit / skip]"
    )
    ans = input_fn("").strip().lower()
    if ans in ("e", "edit"):
        entered = input_fn("pythonpath dirs (space-separated): ").split()
        return entered or suggested
    if ans in ("s", "skip", "n", "no"):
        return None
    return suggested


def format_pythonpath_yaml(dirs: list[str]) -> str:
    """Serialize a `pythonpath:` YAML line matching `format_test_cmd_fast_yaml`'s
    json.dumps idiom, e.g. `["src"]` => `pythonpath: ["src"]\\n`."""
    return f"pythonpath: {json.dumps(dirs)}\n"
