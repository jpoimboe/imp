# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Read-only, lockless gitignore helpers for the nav tools.

Imports ONLY stdlib + anyio (no `imp` imports) so `nav` can import this without
forming a cycle, mirroring limits.py's no-cycle constraint. Every function fails
soft: a non-success git exit, a missing git binary, or an unreadable index
yields a fall-back sentinel (None) so callers drop to their pre-existing
behavior — this layer never makes a search *fail*.
"""

from __future__ import annotations

import os
from pathlib import Path
from subprocess import PIPE

import anyio

# Leaf exceptions that mean "the git subprocess went away", not "imp has a bug".
#
# One physical event — git exiting without draining stdin — reaches us as one of
# THREE types, chosen by anyio's StreamWriterWrapper.send() on stream state:
#   * stream neither closed nor closing -> the bare OSError (BrokenPipeError /
#     ConnectionResetError) is re-raised as-is;
#   * stream already closing            -> BrokenResourceError;
#   * stream closed by us               -> ClosedResourceError.
# The latter two are anyio's own types and are NOT OSError subclasses, so an
# OSError-only filter catches the race only when it lands on the first branch.
# Which branch wins is timing, not semantics: CPython 3.14's asyncio reliably
# takes the "closing" branch where 3.13 took the bare one, turning what had been
# a rare flake into a deterministic failure.
_SUBPROCESS_GONE = (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError)


async def _git(root: Path, *args: str,
               stdin: bytes | None = None) -> tuple[int, bytes]:
    """Run `git -C <root> <args>` on the host via anyio.run_process (a real,
    cancellable await point). Returns (returncode, stdout-bytes). A missing git
    binary or any spawn error returns (-1, b"") rather than raising.

    A fast-failing git (e.g. check-ignore in a not-a-repo, which exits without
    reading stdin) can die before the stdin feed finishes; run_process surfaces
    that failure wrapped in an ExceptionGroup from its internal task group. A
    group whose leaves are all `_SUBPROCESS_GONE` is the same fail-soft case; a
    group with any other leaf is a real bug and propagates."""
    try:
        result = await anyio.run_process(
            ["git", "-C", str(root), *args],
            input=stdin, stdout=PIPE, stderr=PIPE, check=False,
        )
    except _SUBPROCESS_GONE:
        return -1, b""
    except ExceptionGroup as eg:
        _, rest = eg.split(_SUBPROCESS_GONE)
        if rest is not None:
            raise
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
    """Absolute paths of files under `target` that git does NOT ignore (tracked
    + untracked, minus ignored). None on any git failure so the caller falls back.
    `root` must be the worktree toplevel (the result of `repo_root()`);
    `--full-name` yields repo-root-relative paths resolved against `root`."""
    rc, out = await _git(
        root, "ls-files", "-z", "--full-name",
        "--cached", "--others", "--exclude-standard", "--", str(target),
    )
    if rc != 0:
        return None
    return {root / os.fsdecode(seg) for seg in out.split(b"\x00") if seg}


async def partition_ignored(root: Path, paths: set[Path]) -> set[Path] | None:
    """The subset of `paths` git ignores, via one batched `check-ignore --stdin -z`.
    None on git failure (rc not in {0, 1}); empty set when nothing is ignored or
    `paths` is empty."""
    if not paths:
        return set()
    payload = b"\x00".join(os.fsencode(str(p)) for p in paths)
    rc, out = await _git(root, "check-ignore", "--stdin", "-z", stdin=payload)
    if rc not in (0, 1):
        return None
    echoed = {Path(os.fsdecode(seg)) for seg in out.split(b"\x00") if seg}
    return echoed & paths
