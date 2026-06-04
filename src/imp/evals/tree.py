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


def _slug(fixture: Fixture) -> str:
    return f"eval-{fixture.id}"


def _branch(fixture: Fixture) -> str:
    return f"imp/{_slug(fixture)}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _force_clear(control_root: Path, fixture: Fixture) -> None:
    """Remove the worktree AND its branch (best-effort). Two reasons this must
    FORCE-remove rather than call `remove_worktree`:
    - `remove_worktree` keeps the branch (worktree.py:101-105) -> the next repeat
      of the same fixture would hit BranchExists; the `git branch -D` fixes that.
    - the eval run writes `.imp/eval/...events.jsonl` + transcripts INSIDE the
      worktree (drive_agent uses project_root=worktree), and plain
      `git worktree remove` refuses on untracked files (M6) -> must pass --force."""
    rel = Path(".imp") / "worktrees" / _slug(fixture)
    _git(control_root, "worktree", "remove", "--force", str(rel))
    _git(control_root, "worktree", "prune")
    _git(control_root, "branch", "-D", _branch(fixture))


def materialize_fixture_tree(control_root: Path, fixture: Fixture) -> Path:
    """Create a disposable worktree with the reviewed diff committed at HEAD.
    Returns the worktree path. Raises TreeReconstructionError on a non-existent
    base or a non-applying diff. A stale branch/worktree (prior crash, or the
    prior repeat -- see _force_clear) is cleared and creation retried once.
    BranchExists is a bare Exception (worktree.py:25), NOT a RuntimeError, so it
    must be caught explicitly."""
    slug = _slug(fixture)
    try:
        wt = create_worktree(control_root, slug, fixture.base_sha)
    except BranchExists:
        _force_clear(control_root, fixture)
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
        apply = subprocess.run(
            ["git", "apply", "--whitespace=nowarn"],
            cwd=wt,
            input=fixture.diff,
            capture_output=True,
            text=True,
        )
        if apply.returncode != 0:
            raise TreeReconstructionError(
                f"diff did not apply for {fixture.id}: {apply.stderr.strip()}"
            )
        _git(wt, "add", "-A")
        commit = _git(wt, "commit", "-m", f"eval fixture {fixture.id} (reviewed change)")
        if commit.returncode != 0:
            raise TreeReconstructionError(
                f"could not commit reviewed diff for {fixture.id}: {commit.stderr.strip()}"
            )
    except BaseException:
        _force_clear(control_root, fixture)
        raise
    return wt


def teardown_fixture_tree(control_root: Path, fixture: Fixture) -> None:
    """Remove the disposable worktree AND delete its branch. Best-effort; safe to
    call after a failed materialize. The branch delete is what lets the next
    repeat of the same fixture re-create the worktree."""
    _force_clear(control_root, fixture)
