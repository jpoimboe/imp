# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Validation and formatting helpers for a project's import roots.

The module validates and formats a project's import roots for the `pythonpath`
shadow written to `imp/config.yaml` (worktree-relative dirs prepended to
gate/agent subprocess PYTHONPATH so an editable-installed package resolves to
the worktree under test, not a stale main checkout).
"""

from __future__ import annotations

import ast
import configparser
import json
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple

_PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def is_python_packaging_project(project_root: Path) -> bool:
    """True when the project declares Python packaging (a pyproject.toml,
    setup.py, or setup.cfg exists). Gates init's import-root detection;
    a project without any of these keeps init's non-Python behavior."""
    return any((project_root / name).is_file() for name in _PACKAGING_FILES)


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


def format_pythonpath_yaml(dirs: list[str] | Literal[False]) -> str:
    """Serialize a `pythonpath:` YAML line matching `format_test_cmd_fast_yaml`'s
    json.dumps idiom, e.g. `["src"]` => `pythonpath: ["src"]\\n`, `False` =>
    `pythonpath: false\\n`."""
    return f"pythonpath: {json.dumps(dirs)}\n"


class ImportRoots(NamedTuple):
    """Detected import roots paired with the provenance label shown on the confirmation screen."""
    roots: list[str]
    provenance: str


_NOT_PYTHON = "not a Python project"
_FROM_LAYOUT = "from the layout on disk"


def _dig(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        node = data.get(key)
        if not isinstance(node, dict):
            return {}
        data = node
    return data


def _string_list(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _parent_dirs(paths: object) -> list[str]:
    return [str(PurePosixPath(p.replace("\\", "/")).parent)
            for p in _string_list(paths) if p.strip()]


def _package_dir_roots(value: object) -> list[str]:
    """setuptools `package_dir`: the `""` key names the import root directly; a
    named package maps to its own directory, whose PARENT is the import root."""
    if not isinstance(value, dict):
        return []
    root = value.get("")
    if isinstance(root, str):
        return [root]
    return _parent_dirs(list(value.values()))


def _poetry_roots(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry["from"] for entry in value
            if isinstance(entry, dict) and isinstance(entry.get("from"), str)]


def _declared_in_pyproject(project_root: Path) -> list[str]:
    """Import roots a packaging backend declares in pyproject.toml — a shallow
    read of declared paths, not a reimplementation of the backends. Empty when
    nothing is declared or the file doesn't parse."""
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(errors="ignore"))
    except tomllib.TOMLDecodeError:
        return []
    tool = _dig(data, "tool")
    setuptools = _dig(tool, "setuptools")
    for roots in (
        _parent_dirs(_dig(tool, "hatch", "build", "targets", "wheel").get("packages")),
        _package_dir_roots(setuptools.get("package-dir")),
        _parent_dirs(setuptools.get("packages")),
        _poetry_roots(_dig(tool, "poetry").get("packages")),
        _string_list(_dig(tool, "pytest", "ini_options").get("pythonpath")),
    ):
        if roots:
            return roots
    return []


def _declared_in_setup_cfg(project_root: Path) -> list[str]:
    """setuptools' `[options] package_dir`, else a `[tool:pytest] pythonpath` —
    the two setup.cfg declarations that name an import root."""
    path = project_root / "setup.cfg"
    if not path.is_file():
        return []
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(path.read_text(errors="ignore"))
    except configparser.Error:
        return []
    mapping: dict[str, str] = {}
    for line in parser.get("options", "package_dir", fallback="").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            mapping[key.strip()] = value.strip()
    roots = _package_dir_roots(mapping)
    if roots:
        return roots
    return parser.get("tool:pytest", "pythonpath", fallback="").split()


def _declared_in_setup_py(project_root: Path) -> list[str]:
    """A literal `package_dir={...}` keyword read out of setup.py's AST. The
    file is parsed, never executed, so a computed value is skipped."""
    path = project_root / "setup.py"
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "package_dir":
                continue
            try:
                value = ast.literal_eval(kw.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            roots = _package_dir_roots(value)
            if roots:
                return roots
    return []


_DECLARED_SOURCES: tuple[tuple[str, Callable[[Path], list[str]]], ...] = (
    ("from pyproject.toml", _declared_in_pyproject),
    ("from setup.cfg", _declared_in_setup_cfg),
    ("from setup.py", _declared_in_setup_py),
)


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
    provenance, for init's confirmation screen and `--use-defaults`. Declared
    package paths win; the layout on disk answers when nothing is declared.
    Every empty outcome is reported as `not a Python project`."""
    if not is_python_packaging_project(project_root):
        return ImportRoots([], _NOT_PYTHON)
    for provenance, reader in _DECLARED_SOURCES:
        declared = validate_detected_roots(project_root, reader(project_root))
        if declared:
            return ImportRoots(declared, provenance)
    probed = validate_detected_roots(project_root, _probe_layout(project_root))
    if probed:
        return ImportRoots(probed, _FROM_LAYOUT)
    return ImportRoots([], _NOT_PYTHON)
