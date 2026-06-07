"""Materialize a fixture's reviewed tree in a disposable git worktree so the
review agent's `git show HEAD` and repo-wide grep behave exactly as in a live run
(spec Harness §). Strategy: create a worktree at `base_sha` (reuses the real
`create_worktree` seam), apply the fixture's stored diff, and commit it — so HEAD
is the reviewed change and HEAD~1 is the base. The harness applies the diff rather
than `git cherry-pick head_sha` because a mined fixture's diff may not correspond
to a single contiguous commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.engine.worktree import BranchExists, create_worktree
from imp.evals.models import Fixture


class TreeReconstructionError(RuntimeError):
    """The fixture's base tree could not be checked out, or its diff did not
    apply. Caller logs + drops the episode (spec Risks §, never silently skip)."""


def _slug(fixture: Fixture, task_index: int | None = None, unique: str = "") -> str:
    # `unique` isolates concurrent materializations of the SAME fixture (two repeats
    # in flight at once) onto distinct worktree slugs — without it they collide on
    # `eval-{id}` and crash under concurrency (TreeReconstructionError).
    base = f"eval-{fixture.id}"
    if unique:
        base = f"{base}-{unique}"
    return base if task_index is None else f"{base}-t{task_index}"


def _branch(fixture: Fixture, task_index: int | None = None, unique: str = "") -> str:
    return f"imp/{_slug(fixture, task_index, unique)}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _force_clear(
    control_root: Path, fixture: Fixture, task_index: int | None = None, unique: str = ""
) -> None:
    """Remove the worktree AND its branch (best-effort). Two reasons this must
    FORCE-remove rather than call `remove_worktree`:
    - `remove_worktree` keeps the branch (worktree.py:101-105) -> the next repeat
      of the same fixture would hit BranchExists; the `git branch -D` fixes that.
    - the eval run writes `.imp/eval/...events.jsonl` + transcripts INSIDE the
      worktree (drive_agent uses project_root=worktree), and plain
      `git worktree remove` refuses on untracked files (M6) -> must pass --force.

    `task_index=None` is today's `eval-{id}` slug; an int selects the per-task
    `eval-{id}-t{i}` slug so N task trees of one fixture clear independently."""
    rel = Path(".imp") / "worktrees" / _slug(fixture, task_index, unique)
    _git(control_root, "worktree", "remove", "--force", str(rel))
    _git(control_root, "worktree", "prune")
    _git(control_root, "branch", "-D", _branch(fixture, task_index, unique))


def _apply_diff(wt: Path, diff: str, fixture_id: str) -> None:
    """Apply one diff with the SAME `git apply --whitespace=nowarn` the cumulative
    path uses — a whitespace-fixup task could otherwise diverge the per-commit tree
    hash from the cumulative one (the divergence the M3 invariant guards)."""
    apply = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"],
        cwd=wt,
        input=diff,
        capture_output=True,
        text=True,
    )
    if apply.returncode != 0:
        raise TreeReconstructionError(
            f"diff did not apply for {fixture_id}: {apply.stderr.strip()}"
        )


def _commit(wt: Path, fixture_id: str, message: str) -> None:
    _git(wt, "add", "-A")
    commit = _git(wt, "commit", "-m", message)
    if commit.returncode != 0:
        raise TreeReconstructionError(
            f"could not commit for {fixture_id}: {commit.stderr.strip()}"
        )


def materialize_fixture_tree(control_root: Path, fixture: Fixture, unique: str = "") -> Path:
    """Create a disposable worktree with the reviewed diff committed at HEAD.
    Returns the worktree path. Raises TreeReconstructionError on a non-existent
    base or a non-applying diff. A stale branch/worktree (prior crash, or the
    prior repeat -- see _force_clear) is cleared and creation retried once.
    BranchExists is a bare Exception (worktree.py:25), NOT a RuntimeError, so it
    must be caught explicitly. `unique` (a per-call token) isolates concurrent
    materializations of the same fixture so they don't collide on one slug."""
    slug = _slug(fixture, unique=unique)
    try:
        wt = create_worktree(control_root, slug, fixture.base_sha)
    except BranchExists:
        _force_clear(control_root, fixture, unique=unique)
        try:
            wt = create_worktree(control_root, slug, fixture.base_sha)
        except (RuntimeError, BranchExists) as e:
            raise TreeReconstructionError(
                f"could not create worktree for {fixture.id}: {e}"
            ) from e
    except RuntimeError as e:  # `git worktree add` failed (bad base sha, etc.)
        raise TreeReconstructionError(
            f"could not check out base {fixture.base_sha[:12]} for {fixture.id}: {e}"
        ) from e

    # Everything after creation force-clears on ANY failure (checked or
    # unexpected) so a partial materialize never leaks a worktree/branch.
    try:
        _git(wt, "config", "user.email", "eval@imp")
        _git(wt, "config", "user.name", "imp-eval")
        _apply_diff(wt, fixture.diff, fixture.id)
        _commit(wt, fixture.id, f"eval fixture {fixture.id} (reviewed change)")
    except BaseException:
        _force_clear(control_root, fixture, unique=unique)
        raise
    return wt


def materialize_task_tree(
    control_root: Path, fixture: Fixture, task_index: int, unique: str = ""
) -> Path:
    """Per-task tree for task `task_index`: HEAD is EXACTLY tasks[task_index].diff
    (so `git show HEAD` shows only that task), HEAD~1 is base + tasks[0..task_index-1],
    and the working tree is base + tasks[0..task_index]. Building the prefix fresh for
    each task re-applies it N times across the loop (O(N^2) applies) — fine for the
    curated N<=~3 set. Uses the task-indexed `eval-{id}-t{i}` slug so the N task trees
    of one fixture never collide on BranchExists; `unique` further isolates concurrent
    materializations of the same (fixture, task)."""
    slug = _slug(fixture, task_index, unique)
    try:
        wt = create_worktree(control_root, slug, fixture.base_sha)
    except BranchExists:
        _force_clear(control_root, fixture, task_index, unique)
        try:
            wt = create_worktree(control_root, slug, fixture.base_sha)
        except (RuntimeError, BranchExists) as e:
            raise TreeReconstructionError(
                f"could not create worktree for {fixture.id} t{task_index}: {e}"
            ) from e
    except RuntimeError as e:  # `git worktree add` failed (bad base sha, etc.)
        raise TreeReconstructionError(
            f"could not check out base {fixture.base_sha[:12]} for {fixture.id}: {e}"
        ) from e

    # Force-clear on ANY post-creation failure so a partial materialize never
    # leaks a worktree/branch.
    try:
        _git(wt, "config", "user.email", "eval@imp")
        _git(wt, "config", "user.name", "imp-eval")
        # Base commit = base + tasks[0..i-1] (one commit, so HEAD~1 is the prefix).
        # Skipped at task 0 (HEAD~1 stays base).
        for j in range(task_index):
            _apply_diff(wt, fixture.tasks[j].diff, fixture.id)
        if task_index > 0:
            _commit(
                wt,
                fixture.id,
                f"eval fixture {fixture.id} base+tasks[0..{task_index - 1}]",
            )
        # Second commit = ONLY tasks[i], so `git show HEAD` == tasks[i].diff.
        _apply_diff(wt, fixture.tasks[task_index].diff, fixture.id)
        _commit(wt, fixture.id, f"eval fixture {fixture.id} task {task_index}")
    except BaseException:
        _force_clear(control_root, fixture, task_index, unique)
        raise
    return wt


def teardown_fixture_tree(
    control_root: Path, fixture: Fixture, *, task_index: int | None = None, unique: str = ""
) -> None:
    """Remove the disposable worktree AND delete its branch. Best-effort; safe to
    call after a failed materialize. The branch delete is what lets the next
    repeat of the same fixture re-create the worktree. `task_index=None` is today's
    `eval-{id}` slug; an int selects the per-task `eval-{id}-t{i}` slug; `unique`
    selects the concurrency-isolated slug."""
    _force_clear(control_root, fixture, task_index, unique)
