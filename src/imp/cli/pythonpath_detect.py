"""Validation and formatting helpers for a project's import roots.

The module validates and formats a project's import roots for the `pythonpath`
shadow written to `imp/config.yaml` (worktree-relative dirs prepended to
gate/agent subprocess PYTHONPATH so an editable-installed package resolves to
the worktree under test, not a stale main checkout), and offers the offline
provider-credential readiness check (`resolve_detection_provider`).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from imp.providers.effective import load_effective_config
from imp.providers.resolve import available_providers

_PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def is_python_packaging_project(project_root: Path) -> bool:
    """True when the project declares Python packaging (a pyproject.toml,
    setup.py, or setup.cfg exists). Gates init's agent detection + readiness
    checks; a project without any of these keeps init's non-Python behavior."""
    return any((project_root / name).is_file() for name in _PACKAGING_FILES)


def resolve_detection_provider(
    providers: list[str], *, tier: str,
) -> tuple[str, str] | None:
    """The first provider in `providers` (pick order) with a usable credential,
    paired with its `tier` model. None when no picked provider is usable — the
    offline credential check for init's readiness gate. `tier` is required: a
    default here would silently outrank the caller's declared tier."""
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


class ImportRoots(NamedTuple):
    """Detected import roots paired with the provenance label shown on the confirmation screen."""
    roots: list[str]
    provenance: str


_NOT_PYTHON = "not a Python project"
_FROM_LAYOUT = "from the layout on disk"


def _probe_layout(project_root: Path) -> list[str]:
    """`src` when it directly holds a package dir, else the repo root when it does —
    empty when neither."""
    for candidate in ("src", "."):
        base = project_root / candidate
        if not base.is_dir():
            continue
        if any((child / "__init__.py").is_file()
               for child in base.iterdir() if child.is_dir()):
            return [candidate]
    return []


def detect_import_roots(project_root: Path) -> ImportRoots:
    """The worktree-relative import roots for `project_root` with their
    provenance, for init's confirmation screen and `--use-defaults`. Every
    empty outcome is reported as `not a Python project`."""
    if not is_python_packaging_project(project_root):
        return ImportRoots([], _NOT_PYTHON)
    probed = validate_detected_roots(project_root, _probe_layout(project_root))
    if probed:
        return ImportRoots(probed, _FROM_LAYOUT)
    return ImportRoots([], _NOT_PYTHON)
