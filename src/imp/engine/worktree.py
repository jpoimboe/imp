"""All git mechanics for per-Change worktrees (spec §4), behind a small
interface so Change, the executor, and the CLI stay git-agnostic. Every
shared-repo mutation runs under RepoGitLock.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import filelock


class PreconditionError(Exception):
    """imp new ran in a repo that can't host a worktree (spec §5.1)."""


class BranchExists(Exception):
    """The slug's branch imp/<slug> already exists (collision)."""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )


def _git_ok(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def find_control_root(start: Path) -> Path:
    """Resolve the main checkout from anywhere (incl. inside a worktree) via
    `git rev-parse --git-common-dir` -> its parent (spec §3, §5.3)."""
    proc = _git(start, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        raise PreconditionError(f"{start} is not a git repository")
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = (start / common)
    return common.resolve().parent


def check_preconditions(root: Path) -> None:
    """Hard requirements git worktree add + branch creation need (spec §5.1)."""
    if _git(root, "rev-parse", "--git-dir").returncode != 0:
        raise PreconditionError(f"{root} is not a git repository (run `git init`)")
    if _git(root, "rev-parse", "HEAD").returncode != 0:
        raise PreconditionError(
            "repository has no commits yet; make an initial commit first"
        )
    head = _git_ok(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if head == "HEAD":
        raise PreconditionError(
            "HEAD is detached; check out a branch before `imp new`"
        )


def current_branch(root: Path) -> str:
    return _git_ok(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def branch_exists(control_root: Path, branch: str) -> bool:
    """Public so the CLI can pre-check slug availability (spec §3.1 — git
    branch-uniqueness IS the collision check)."""
    return _git(
        control_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0


def create_worktree(control_root: Path, slug: str, base: str) -> Path:
    """`git worktree add -b imp/<slug> .imp/worktrees/<slug> <base>` (spec §4).
    Branch collision surfaces as BranchExists so the caller can retry (random
    slug) or report a clean error (title-derived slug)."""
    rel = Path(".imp") / "worktrees" / slug
    branch = f"imp/{slug}"
    with RepoGitLock(control_root).acquire():
        if branch_exists(control_root, branch):
            raise BranchExists(branch)
        proc = _git(control_root, "worktree", "add", "-b", branch, str(rel), base)
        if proc.returncode != 0:
            # already-exists races collapse to BranchExists; anything else bubbles
            if "already exists" in proc.stderr:
                raise BranchExists(branch)
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    return (control_root / rel).resolve()


def remove_worktree(control_root: Path, slug: str) -> None:
    """`git worktree remove .imp/worktrees/<slug>`. Branch is KEPT (spec §4)."""
    rel = Path(".imp") / "worktrees" / slug
    with RepoGitLock(control_root).acquire():
        _git_ok(control_root, "worktree", "remove", str(rel))


class RepoGitLock:
    """Short-held file lock in the control root, acquired around every
    shared-repo mutation (spec §4, §7). NOT held for the duration of a run."""

    def __init__(self, control_root: Path) -> None:
        self.path = control_root / ".imp" / "repo.lock"
        # Create .imp/ up front so correctness doesn't rely on filelock creating
        # parent dirs (version-dependent behavior).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fl = filelock.FileLock(str(self.path) + ".filelock")

    @contextmanager
    def acquire(self, timeout: float = 60.0):  # type: ignore[no-untyped-def]
        self._fl.acquire(timeout=timeout)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()))
            yield self
        finally:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._fl.release()
