"""Heuristic detection of a project's import roots for the PYTHONPATH shadow.

Proposes an `imp/project.yaml` `pythonpath` value at `imp init` (config-birth):
worktree-relative directories prepended to gate/agent subprocess PYTHONPATH so an
editable-installed package resolves to the worktree under test, not a stale main
checkout. Suggestions only — the user accepts, edits, or skips.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from imp.providers.effective import load_effective_config
from imp.providers.resolve import available_providers


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


_PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def is_python_packaging_project(project_root: Path) -> bool:
    """True when the project declares Python packaging (a pyproject.toml,
    setup.py, or setup.cfg exists). Gates init's agent detection + readiness
    checks; a project without any of these keeps init's non-Python behavior."""
    return any((project_root / name).is_file() for name in _PACKAGING_FILES)


def resolve_detection_provider(
    providers: list[str], *, tier: str = "medium",
) -> tuple[str, str] | None:
    """The first provider in `providers` (pick order) with a usable credential,
    paired with its `tier` model. None when no picked provider is usable — the
    offline credential check for init's readiness gate."""
    cfg = load_effective_config().config
    usable = available_providers(replace(cfg, providers=tuple(providers)))
    if not usable:
        return None
    provider = usable[0]
    return provider, cfg.tiers[tier][provider]


def _normalize_root(entry: str) -> str | None:
    """Normalize one reported root to a worktree-relative form, or None when it
    is malformed (absolute, or escaping the worktree with `..`)."""
    e = entry.strip().replace("\\", "/")
    if e.startswith("/"):
        return None
    e = e.rstrip("/")
    while e.startswith("./"):
        e = e[2:]
    if e in ("", "."):
        return "."
    if ".." in Path(e).parts:
        return None
    return e


def validate_detected_roots(project_root: Path, roots: list[str]) -> list[str]:
    """Normalize and confirm each reported root against disk: existing dirs
    under `project_root` only, no absolute paths, no `..`, de-duplicated. `"."`
    (the repo root) is always valid. Entries that fail are dropped."""
    out: list[str] = []
    for entry in roots:
        norm = _normalize_root(entry)
        if norm is None or norm in out:
            continue
        if (project_root / norm).is_dir():
            out.append(norm)
    return out


def format_pythonpath_yaml(dirs: list[str]) -> str:
    """Serialize a `pythonpath:` YAML line matching `format_test_cmd_fast_yaml`'s
    json.dumps idiom, e.g. `["src"]` => `pythonpath: ["src"]\\n`."""
    return f"pythonpath: {json.dumps(dirs)}\n"
