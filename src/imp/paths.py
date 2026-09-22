# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Where imp keeps state on disk: the per-machine home, and how a path is shown
to a user. Deliberately free of `imp.*` module-level imports — engine, CLI,
remote and provider modules all import it."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from itertools import count
from pathlib import Path

# Written into every source snapshot; names the tree the snapshot was copied from.
SOURCE_ROOT_MARKER = "source-root"


def imp_home() -> Path:
    """$IMP_HOME, default ~/.imp — imp's per-machine directory."""
    base = os.environ.get("IMP_HOME") or str(Path.home() / ".imp")
    return Path(base)


def display_path(path: Path) -> str:
    """`~`-relative under $HOME, absolute otherwise."""
    home = Path.home()
    if home in path.parents:
        return f"~/{path.relative_to(home)}"
    return str(path)


_SLUG_DISALLOWED = re.compile(r"[^a-z0-9._-]")
_SLUG_MAX = 64


def _slug(basename: str) -> str:
    return _SLUG_DISALLOWED.sub("-", basename.lower())[:_SLUG_MAX] or "project"


def _state_dir_candidates(root: Path) -> Iterator[Path]:
    projects = imp_home() / "projects"
    base = _slug(root.name)
    yield projects / base
    for n in count(2):
        yield projects / f"{base}-{n}"


def claimed_state_dir(control_root: Path) -> Path | None:
    """This project's state directory if one is already claimed — unlike
    `project_state_dir`, creating and marking nothing. `$IMP_STATE_DIR` still wins
    verbatim, so a hand-pinned directory always reads as claimed."""
    root = control_root.resolve()
    override = os.environ.get("IMP_STATE_DIR")
    if override:
        return _checked_override(Path(override), root)
    for candidate in _state_dir_candidates(root):
        if not candidate.exists():
            return None
        marker = candidate / "repo"
        if marker.is_file() and marker.read_text().strip() == str(root):
            return candidate
    return None  # unreachable — the candidate stream is unbounded, which mypy can't infer


def project_state_dir(control_root: Path) -> Path:
    """This project's state directory — where its quests, worktrees and archive
    live. `$IMP_STATE_DIR` wins verbatim when set. Otherwise resolution claims:
    the first call for a control root creates the slug directory under
    `imp_home()/projects` and writes its `repo` marker. A slug already claimed by
    a different control root is skipped for `<slug>-2`, `-3`, …"""
    root = control_root.resolve()
    while True:
        claimed = claimed_state_dir(root)
        if claimed is not None:
            return claimed
        candidate = next(c for c in _state_dir_candidates(root) if not c.exists())
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue  # another process claimed it between the two calls
        (candidate / "repo").write_text(f"{root}\n")
        return candidate


def _checked_override(state_dir: Path, control_root: Path) -> Path:
    """$IMP_STATE_DIR verbatim — creating nothing, marking nothing. Refuses only when the
    directory already carries another checkout's `repo` marker, which the sandbox re-exposes
    read-only through the state mask so an in-sandbox read sees it too. An absent marker
    reads clean: a hand-pinned state dir (or the legacy `_pin` window) carries none and is
    unguarded by choice."""
    marker = state_dir / "repo"
    owner = marker.read_text().strip() if marker.is_file() else None
    if owner is not None and owner != str(control_root):
        # Lazy: keeps this leaf module free of engine imports.
        from imp.engine.worktree import PreconditionError

        raise PreconditionError(
            f"IMP_STATE_DIR={display_path(state_dir)} holds state for "
            f"{display_path(Path(owner))} — refusing to use it here. Fix: to run imp "
            "against a scratch checkout, unset IMP_STATE_DIR and point IMP_HOME at a "
            "throwaway directory."
        )
    return state_dir
