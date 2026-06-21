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


def head_of(control_root: Path, ref: str) -> str:
    """The commit sha `ref` resolves to (e.g. a branch tip)."""
    return _git_ok(control_root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def branch_exists(control_root: Path, branch: str) -> bool:
    """True if refs/heads/<branch> exists. The imp new collision-error guard
    keys on this."""
    return _git(
        control_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0


def commit_count(control_root: Path, change_id: str, base: str) -> int:
    """Commits on imp/<id> not reachable from base — the Change's own work."""
    proc = _git(control_root, "rev-list", "--count", f"{base}..imp/{change_id}")
    if proc.returncode != 0:
        return 0
    return int(proc.stdout.strip())


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


def remove_worktree(control_root: Path, change_id: str, *, force: bool = False) -> None:
    """`git worktree remove .imp/worktrees/<id>`. Branch is KEPT (spec §4).

    Default (force=False) is the merge-teardown behaviour: a plain remove that
    errors on a dirty tree. With force=True (used by `imp abandon`) tolerate a
    dirty tree / stale metadata / an already-absent worktree: retry with
    --force, then prune git's bookkeeping and drop any leftover directory so a
    re-run after a crash converges."""
    rel = Path(".imp") / "worktrees" / change_id
    with RepoGitLock(control_root).acquire():
        if not force:
            _git_ok(control_root, "worktree", "remove", str(rel))
            return
        proc = _git(control_root, "worktree", "remove", "--force", str(rel))
        if proc.returncode != 0:
            _git(control_root, "worktree", "prune")
            shutil.rmtree(control_root / rel, ignore_errors=True)


def delete_branch(control_root: Path, change_id: str) -> None:
    """`git branch -D imp/<id>` (abandon-only; remove_worktree keeps the
    branch). Tolerates an already-deleted branch so abandon is idempotent, and
    needs only the id — callable even when the worktree/change.json is gone
    (the (remove -> delete) crash-recovery path)."""
    with RepoGitLock(control_root).acquire():
        # rc ignored deliberately: tolerates "branch not found" (already
        # deleted — the idempotency we want). The other non-zero case is
        # "used by worktree", which only happens if a caller skips the
        # required remove_worktree-before-delete_branch ordering; abandon's
        # flow always removes first, so that path is unreachable here.
        _git(control_root, "branch", "-D", f"imp/{change_id}")


def destroy_worktree(control_root: Path, change_id: str) -> None:
    """Full teardown: remove the worktree AND delete its branch imp/<id>,
    leaving nothing behind. Used to roll back a failed Change.create — unlike
    remove_worktree, which deliberately KEEPS the branch after a successful run.
    Best-effort git calls (not _git_ok) so a teardown hiccup never masks the
    LoadError that triggered the rollback — a teardown failure may therefore
    leave an orphan rather than raise. Acceptable under V0's single principal."""
    rel = Path(".imp") / "worktrees" / change_id
    branch = f"imp/{change_id}"
    with RepoGitLock(control_root).acquire():
        _git(control_root, "worktree", "remove", "--force", str(rel))
        shutil.rmtree(control_root / rel, ignore_errors=True)
        # If `worktree remove` failed but rmtree deleted the dir, git still has a
        # stale registration that makes `branch -D` fail with "checked out at …",
        # leaking the branch. Prune the stale entry first so the delete succeeds.
        _git(control_root, "worktree", "prune")
        _git(control_root, "branch", "-D", branch)


class RepoGitLock:
    """Short-held file lock in the control root, acquired around every
    shared-repo mutation (spec §4, §7). NOT held for the duration of a run."""

    def __init__(self, control_root: Path) -> None:
        # Lives under .git (the git common dir), NOT .imp: artifact_commit
        # acquires this lock from inside the bwrap sandbox, whose writable set
        # binds back the worktree and <control>/.git but leaves the control
        # root's top-level .imp/ read-only. .git is already rw-bound and is the
        # right scope for a repo-wide lock guarding writes to refs/imp/*.
        self.path = control_root / ".git" / "imp" / "repo.lock"
        # Create the parent up front so correctness doesn't rely on filelock
        # creating parent dirs (version-dependent behavior).
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
    status: Literal["MERGED", "CONFLICT", "REFUSED", "DIRTY"]
    merge_commit: str | None = None
    detail: str | None = None   # git's stderr on a blocking outcome (surfaced to the human)


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


def _merge_subject(title: str, change_id: str, body: str | None) -> str:
    trailer = f"(imp/{change_id})"
    if body:
        return f"Merge {title}\n\n{body.strip()}\n\n{trailer}"
    return f"Merge {title}\n\n{trailer}"


def merge_to_base(
    control_root: Path, change_id: str, base: str, *, title: str,
    body: str | None = None,
    project_root: Path | None = None,
) -> MergeResult:
    """Integrate imp/<id> into <base> (spec §4). B1: git decides on base dirt.
    B3: re-assert UNDER THE LOCK (the race-proof point) that the change worktree
    is clean (DIRTY) when `project_root` is given: the integrator is the last
    committer, so a dirty tree would silently drop its work then be destroyed by
    remove_worktree (H1 net, R5)."""
    branch = f"imp/{change_id}"
    with RepoGitLock(control_root).acquire():
        if project_root is not None and is_dirty(project_root):
            return MergeResult("DIRTY")
        live = _worktree_on_branch(control_root, base)
        if live is not None:
            return _merge_in_place(live, branch, title, change_id, body)
        return _merge_detached(control_root, change_id, branch, base, title, body)


def _merge_in_place(worktree: Path, branch: str, title: str, change_id: str,
                    body: str | None = None) -> MergeResult:
    """Branch 1: advance ref + index + working tree atomically in the live
    checkout. (See the dirty-safety note in merge_to_base.)

    F6: on a non-zero merge, branch on whether a merge is actually in progress
    (MERGE_HEAD) BEFORE deciding to abort. A real conflict starts a merge we must
    abort; a clean refusal (uncommitted base files would be clobbered, unrelated
    histories, …) starts nothing — calling `git merge --abort` there errors ("no
    merge to abort"). MERGE_HEAD answers this locale-independently. git's stderr
    is carried in `detail` so the surfaced reason is truthful for any refusal."""
    subject = _merge_subject(title, change_id, body)
    proc = _git(worktree, "merge", "--no-ff", "-m", subject, branch)
    if proc.returncode == 0:
        commit = _git_ok(worktree, "rev-parse", "HEAD").strip()
        return MergeResult("MERGED", commit)
    detail = (proc.stderr or proc.stdout).strip() or None
    merge_in_progress = _git(
        worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD"
    ).returncode == 0
    if merge_in_progress:
        _git(worktree, "merge", "--abort")
        return MergeResult("CONFLICT", detail=detail)
    return MergeResult("REFUSED", detail=detail)


def _merge_detached(control_root: Path, change_id: str, branch: str, base: str,
                    title: str, body: str | None = None) -> MergeResult:
    """Branch 3: <base> checked out nowhere. Merge in a detached scratch worktree,
    update-ref, remove the scratch."""
    scratch_rel = Path(".imp") / "worktrees" / f".merge-{change_id}"
    scratch = control_root / scratch_rel
    _git(control_root, "worktree", "remove", "--force", str(scratch_rel))
    shutil.rmtree(scratch, ignore_errors=True)
    _git_ok(control_root, "worktree", "add", "--detach", str(scratch_rel), base)
    subject = _merge_subject(title, change_id, body)
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


@contextmanager
def _temp_index(control_root: Path):  # type: ignore[no-untyped-def]
    """Yield a throwaway GIT_INDEX_FILE env dict (file lives under .git/imp/,
    the sandbox-writable git common dir — NOT the read-only .imp/; see
    RepoGitLock).
    Do NOT simplify the unlink dance: a 0-byte index makes `git update-index`
    fail; a *missing* GIT_INDEX_FILE is treated as an empty index — exactly
    what the no-parent bootstrap needs."""
    env = dict(os.environ)
    scratch = control_root / ".git" / "imp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=scratch, delete=False) as tf:
        index_file = tf.name
    os.unlink(index_file)
    env["GIT_INDEX_FILE"] = index_file
    try:
        yield env
    finally:
        Path(index_file).unlink(missing_ok=True)


def _commit_files_to_ref(
    control_root: Path, *, ref: str, prefix: str, files: Mapping[str, Path],
    message: str, extra_tree: tuple[str, str] | None = None,
) -> None:
    """Append one commit to `ref` via plumbing — never checks out the ref.

    The index is seeded from `ref` (so existing content accumulates), then
    `extra_tree=(p, treeish)` — when given — is read in under prefix `p`, then
    each `files` entry is hashed and added at `prefix+name`. The commit is
    parented on `ref` when it already exists (else a root commit). CAS on the
    final update-ref is cheap insurance (moot under the lock)."""
    with RepoGitLock(control_root).acquire(), _temp_index(control_root) as env:
            parent = _git(control_root, "rev-parse", "--verify", "--quiet", ref)
            have_parent = parent.returncode == 0
            if have_parent:
                subprocess.run(["git", "read-tree", ref], cwd=control_root, env=env,
                               check=True, capture_output=True, text=True)
            if extra_tree is not None:
                p, treeish = extra_tree
                subprocess.run(["git", "read-tree", f"--prefix={p}", treeish],
                               cwd=control_root, env=env, check=True,
                               capture_output=True, text=True)
            for name, src in files.items():
                blob = subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"], cwd=control_root,
                    env=env, input=src.read_bytes(), capture_output=True, check=True,
                ).stdout.decode().strip()
                subprocess.run(
                    ["git", "update-index", "--add", "--cacheinfo",
                     f"100644,{blob},{prefix}{name}"],
                    cwd=control_root, env=env, check=True, capture_output=True, text=True)
            tree = subprocess.run(["git", "write-tree"], cwd=control_root, env=env,
                                  check=True, capture_output=True, text=True).stdout.strip()
            args = ["git", "commit-tree", tree, "-m", message]
            if have_parent:
                args += ["-p", ref]
            commit = subprocess.run(args, cwd=control_root, env=env, check=True,
                                    capture_output=True, text=True).stdout.strip()
            cas = ["git", "update-ref", ref, commit]
            cas += [parent.stdout.strip()] if have_parent else []
            subprocess.run(cas, cwd=control_root, check=True,
                           capture_output=True, text=True)


def artifact_commit(
    control_root: Path, change_id: str, files: Mapping[str, Path], *, message: str,
) -> None:
    """Append one commit to the per-change artifact lineage
    refs/imp/journal-wip/<id>, accumulating on the prior wip tip. Files sit FLAT
    at the wip tree root (e.g. spec/<id>.md, plans/<id>-plan-N.md)."""
    _commit_files_to_ref(
        control_root, ref=f"refs/imp/journal-wip/{change_id}", prefix="",
        files=files, message=message)


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
    the inner .imp/changes/ state must never be captured by `git add -A`."""
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
