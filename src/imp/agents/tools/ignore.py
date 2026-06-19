"""Read-only, lockless gitignore helpers for the nav tools.

Imports ONLY stdlib + anyio (no `imp` imports) so `nav` can import this without
forming a cycle, mirroring limits.py's no-cycle constraint. Every function fails
soft: a non-success git exit, a missing git binary, or an unreadable index
yields a fall-back sentinel (None) so callers drop to their pre-existing
behavior — this layer never makes a search *fail*.
"""

from __future__ import annotations

from pathlib import Path
from subprocess import PIPE

import anyio


async def _git(root: Path, *args: str,
               stdin: bytes | None = None) -> tuple[int, bytes]:
    """Run `git -C <root> <args>` on the host via anyio.run_process (a real,
    cancellable await point). Returns (returncode, stdout-bytes). A missing git
    binary or any spawn error returns (-1, b"") rather than raising."""
    try:
        result = await anyio.run_process(
            ["git", "-C", str(root), *args],
            input=stdin, stdout=PIPE, stderr=PIPE, check=False,
        )
    except OSError:
        return -1, b""
    return result.returncode, result.stdout


async def repo_root(path: Path) -> Path | None:
    """The enclosing worktree top-level, or None outside a repo / when git is
    unavailable."""
    rc, out = await _git(path, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None
    line = out.decode("utf-8", "replace").strip()
    return Path(line) if line else None


async def is_ignored(root: Path, path: Path) -> bool:
    """True when git ignores `path`. check-ignore exit 0 => ignored, 1 => not
    ignored, anything else (error / no git) => not ignored (caller takes the
    normal path)."""
    rc, _ = await _git(root, "check-ignore", "-q", "--", str(path))
    return rc == 0


async def list_unignored(root: Path, target: Path) -> set[Path] | None:
    """Placeholder filled in Task 2."""
    raise NotImplementedError


async def partition_ignored(root: Path, paths: set[Path]) -> set[Path] | None:
    """Placeholder filled in Task 2."""
    raise NotImplementedError
