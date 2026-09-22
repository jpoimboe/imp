# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-run source snapshot: the daemon copies the imp it is running into the quest
home and executes from there, so a merge to the base branch cannot change the code a
live run — or the in-sandbox `imp` its agents invoke — executes."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import imp
from imp.engine.state import lock_is_live
from imp.engine.worktree import RepoGitLock
from imp.paths import SOURCE_ROOT_MARKER

PINNED_SRC_DIRNAME = "pinned-src"
OWNER_PID_MARKER = "owner-pid"
_NO_BYTECODE = shutil.ignore_patterns("__pycache__")


def pin_source_snapshot(quest_dir: Path, control_root: Path) -> Path:
    """Copy the running imp and impty packages into <quest_dir>/pinned-src/<token>/ and
    return that directory. Raises OSError when the snapshot cannot be made."""
    running_pkg = Path(imp.__file__).resolve().parent
    source_root = running_pkg.parent
    impty_pkg = source_root / "impty"
    if not (impty_pkg / "__init__.py").is_file():
        raise FileNotFoundError(
            f"no impty package beside the running imp at {running_pkg}")
    quest_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=quest_dir, prefix="pinning-"))
    try:
        # The lock merge_to_base takes: a merge either finishes before the copy or waits.
        with RepoGitLock(control_root).acquire():
            shutil.copytree(running_pkg, staging / "imp", ignore=_NO_BYTECODE)
            shutil.copytree(impty_pkg, staging / "impty", ignore=_NO_BYTECODE)
        (staging / SOURCE_ROOT_MARKER).write_text(f"{source_root}\n")
        (staging / OWNER_PID_MARKER).write_text(f"{os.getpid()}\n")
        # uuid4 name: the rename has no destination to overwrite, so a snapshot appears
        # under pinned-src/ only fully formed and no pin can clobber another's tree.
        published = quest_dir / PINNED_SRC_DIRNAME / uuid4().hex
        published.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, published)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _reap_dead_snapshots(published)
    return published


def _reap_dead_snapshots(own: Path) -> None:
    """Delete published siblings whose owner pid is gone. Never our own, never a
    temporary build directory (a build in flight looks identical to an abandoned one),
    and never at the cost of failing the pin: reaping is a disk concern only."""
    try:
        for sibling in own.parent.iterdir():
            if sibling == own or lock_is_live(sibling / OWNER_PID_MARKER):
                continue
            shutil.rmtree(sibling, ignore_errors=True)
    except OSError:
        return
