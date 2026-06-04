"""All git mechanics for per-Change worktrees (spec §4), behind a small
interface so Change, the executor, and the CLI stay git-agnostic. Every
shared-repo mutation runs under RepoGitLock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import filelock


class PreconditionError(Exception):
    """imp new ran in a repo that can't host a worktree (spec §5.1)."""


class BranchExists(Exception):
    """The branch imp/<id> already exists (collision)."""


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
    """True if refs/heads/<branch> exists. The imp new collision-error guard
    keys on this."""
    return _git(
        control_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0


def create_worktree(control_root: Path, change_id: str, base: str) -> Path:
    """`git worktree add -b imp/<id> .imp/worktrees/<id> <base>` (spec §4).
    Branch collision surfaces as BranchExists — the caller reports a clean error
    (there is no retry; the random id is the uniqueness guarantee)."""
    rel = Path(".imp") / "worktrees" / change_id
    branch = f"imp/{change_id}"
    with RepoGitLock(control_root).acquire():
        if branch_exists(control_root, branch):
            raise BranchExists(branch)
        proc = _git(control_root, "worktree", "add", "-b", branch, str(rel), base)
        if proc.returncode != 0:
            if "already exists" in proc.stderr:
                raise BranchExists(branch)
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    return (control_root / rel).resolve()


def remove_worktree(control_root: Path, change_id: str) -> None:
    """`git worktree remove .imp/worktrees/<id>`. Branch is KEPT (spec §4)."""
    rel = Path(".imp") / "worktrees" / change_id
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


@dataclass(frozen=True)
class MergeResult:
    status: Literal["MERGED", "CONFLICT", "BASE_DIRTY"]
    merge_commit: str | None = None


def _worktree_on_branch(control_root: Path, base: str) -> Path | None:
    """Parse `git worktree list --porcelain`; return the worktree path checked
    out on refs/heads/<base>, or None if base is checked out nowhere."""
    out = _git_ok(control_root, "worktree", "list", "--porcelain")
    cur: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{base}" and cur is not None:
            return cur
    return None


def is_dirty(worktree: Path) -> bool:
    """True if the worktree has staged/unstaged/untracked changes. Public so
    `_complete` can refuse to merge a Change with uncommitted work (spec relies
    on agents committing incrementally — `.imp/` is gitignored, so this only
    sees real tracked/untracked project files)."""
    return bool(_git_ok(worktree, "status", "--porcelain").strip())


def merge_to_base(control_root: Path, change_id: str, base: str, *, title: str) -> MergeResult:
    """Integrate imp/<id> into <base> without ever desyncing a live checkout
    (spec §4). The merge subject uses the human title; the id appears in the body
    for branch traceability."""
    branch = f"imp/{change_id}"
    with RepoGitLock(control_root).acquire():
        live = _worktree_on_branch(control_root, base)
        if live is not None:
            if is_dirty(live):
                return MergeResult("BASE_DIRTY")
            return _merge_in_place(live, branch, title, change_id)
        return _merge_detached(control_root, change_id, branch, base, title)


def _merge_in_place(worktree: Path, branch: str, title: str, change_id: str) -> MergeResult:
    """Branch 1: advance ref + index + working tree atomically in the live
    checkout. (See the dirty-safety note in merge_to_base.)"""
    subject = f"Merge {title}\n\n(imp/{change_id})"
    proc = _git(worktree, "merge", "--no-ff", "-m", subject, branch)
    if proc.returncode != 0:
        _git(worktree, "merge", "--abort")
        return MergeResult("CONFLICT")
    commit = _git_ok(worktree, "rev-parse", "HEAD").strip()
    return MergeResult("MERGED", commit)


def _merge_detached(control_root: Path, change_id: str, branch: str, base: str,
                    title: str) -> MergeResult:
    """Branch 3: <base> checked out nowhere. Merge in a detached scratch worktree,
    update-ref, remove the scratch."""
    scratch_rel = Path(".imp") / "worktrees" / f".merge-{change_id}"
    scratch = control_root / scratch_rel
    _git(control_root, "worktree", "remove", "--force", str(scratch_rel))
    shutil.rmtree(scratch, ignore_errors=True)
    _git_ok(control_root, "worktree", "add", "--detach", str(scratch_rel), base)
    subject = f"Merge {title}\n\n(imp/{change_id})"
    try:
        proc = _git(scratch, "merge", "--no-ff", "-m", subject, branch)
        if proc.returncode != 0:
            _git(scratch, "merge", "--abort")
            return MergeResult("CONFLICT")
        commit = _git_ok(scratch, "rev-parse", "HEAD").strip()
        _git_ok(control_root, "update-ref", f"refs/heads/{base}", commit)
        return MergeResult("MERGED", commit)
    finally:
        _git(control_root, "worktree", "remove", "--force", str(scratch_rel))


def journal_commit(control_root: Path, change_id: str, files: Mapping[str, Path]) -> None:
    """Append one commit to refs/imp/journal (spec §4, §6) via plumbing — never
    checks out the ref. Files land under <id>/ in the journal tree. On the
    very first journaled Change the ref doesn't exist, so commit-tree runs with
    no -p (a root commit, starting the orphan history)."""
    with RepoGitLock(control_root).acquire():
        env = dict(os.environ)
        # Keep the transient index inside .imp/ (gitignored) so it never shows as
        # untracked even momentarily. Do NOT "simplify" the unlink dance below.
        with tempfile.NamedTemporaryFile(dir=control_root / ".imp", delete=False) as tf:
            index_file = tf.name
        # CRITICAL: a 0-byte index makes `git update-index` fail with "index file
        # smaller than expected". git treats a *missing* GIT_INDEX_FILE as an empty
        # index, which is what the bootstrap (no-parent) path needs. So unlink the
        # placeholder before any git command touches it.
        os.unlink(index_file)
        env["GIT_INDEX_FILE"] = index_file
        try:
            parent = _git(control_root, "rev-parse", "--verify", "--quiet",
                          "refs/imp/journal")
            have_parent = parent.returncode == 0
            # Seed the temp index with the existing journal tree so prior ids
            # survive (read-tree is a no-op miss when the ref is absent).
            if have_parent:
                subprocess.run(["git", "read-tree", "refs/imp/journal"],
                               cwd=control_root, env=env, check=True,
                               capture_output=True, text=True)
            for name, src in files.items():
                blob = subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"], cwd=control_root,
                    env=env, input=src.read_bytes(), capture_output=True, check=True,
                ).stdout.decode().strip()
                subprocess.run(
                    ["git", "update-index", "--add", "--cacheinfo",
                     f"100644,{blob},{change_id}/{name}"],
                    cwd=control_root, env=env, check=True, capture_output=True, text=True,
                )
            tree = subprocess.run(["git", "write-tree"], cwd=control_root, env=env,
                                  check=True, capture_output=True, text=True).stdout.strip()
            args = ["git", "commit-tree", tree, "-m", f"journal: {change_id}"]
            if have_parent:
                args += ["-p", "refs/imp/journal"]
            commit = subprocess.run(args, cwd=control_root, env=env, check=True,
                                    capture_output=True, text=True).stdout.strip()
            old = parent.stdout.strip() if have_parent else ""
            # CAS for cheap insurance (moot under the lock).
            cas = ["git", "update-ref", "refs/imp/journal", commit]
            cas += [old] if have_parent else []
            subprocess.run(cas, cwd=control_root, check=True,
                           capture_output=True, text=True)
        finally:
            Path(index_file).unlink(missing_ok=True)


def _common_dir(control_root: Path) -> Path:
    out = _git_ok(control_root, "rev-parse", "--git-common-dir").strip()
    p = Path(out)
    return p if p.is_absolute() else (control_root / p).resolve()


def _imp_in_exclude(control_root: Path) -> bool:
    ex = _common_dir(control_root) / "info" / "exclude"
    return ex.exists() and ".imp/" in {ln.strip() for ln in ex.read_text().splitlines()}


def _imp_in_committed_gitignore(control_root: Path) -> bool:
    show = _git(control_root, "cat-file", "-p", "HEAD:.gitignore")
    return show.returncode == 0 and ".imp/" in {
        ln.strip() for ln in show.stdout.splitlines()
    }


def _imp_ignored_durably(control_root: Path) -> bool:
    """True only when the ignore reaches a freshly checked-out worktree: it is
    either committed in .gitignore or present in the shared info/exclude (spec
    §8). A working-tree-only (uncommitted) .gitignore rule does NOT count — a new
    worktree checks out base HEAD's committed .gitignore, not the control root's
    working copy."""
    return _imp_in_exclude(control_root) or _imp_in_committed_gitignore(control_root)


def _write_exclude(control_root: Path) -> None:
    ex = _common_dir(control_root) / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    text = ex.read_text() if ex.exists() else ""
    if ".imp/" not in {ln.strip() for ln in text.splitlines()}:
        sep = "" if text.endswith("\n") or not text else "\n"
        ex.write_text(text + sep + ".imp/\n")


def ensure_imp_ignored(control_root: Path) -> None:
    """Guarantee .imp/ is ignored in a way fresh worktrees inherit (spec §8).
    Correctness depends on it: nested worktrees must not show as untracked, and
    the inner .imp/change/ state must never be captured by `git add -A`."""
    if _imp_ignored_durably(control_root):
        return
    with RepoGitLock(control_root).acquire():
        if _imp_ignored_durably(control_root):  # someone else repaired it
            return
        # Preferred: commit ONLY .gitignore so the rule travels into every
        # worktree checkout and is shared with collaborators.
        gi = control_root / ".gitignore"
        text = gi.read_text() if gi.exists() else ""
        if ".imp/" not in {ln.strip() for ln in text.splitlines()}:
            sep = "" if text.endswith("\n") or not text else "\n"
            gi.write_text(text + sep + "# imp internal state\n.imp/\n")
        _git(control_root, "add", "--", ".gitignore")
        _git(control_root, "commit", "-m", "chore: gitignore .imp/", "--", ".gitignore")
        if _imp_in_committed_gitignore(control_root):
            return
        # Fallback: shared info/exclude (no commit, no ref move).
        _write_exclude(control_root)
        if not _imp_ignored_durably(control_root):
            raise PreconditionError(
                "could not ensure .imp/ is git-ignored; add `.imp/` to .gitignore "
                "and commit it, then re-run"
            )
