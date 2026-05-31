# tests/test_worktree.py
import subprocess
from pathlib import Path

import pytest

from imp.engine import worktree as wt


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main` and `.imp/` gitignored."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def test_find_control_root_from_subdir(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert wt.find_control_root(sub) == tmp_path.resolve()


def test_preconditions_ok(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.check_preconditions(tmp_path)  # does not raise


def test_preconditions_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(wt.PreconditionError, match="not a git repository"):
        wt.check_preconditions(tmp_path)


def test_preconditions_no_commits(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    with pytest.raises(wt.PreconditionError, match="no commits"):
        wt.check_preconditions(tmp_path)


def test_preconditions_detached_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "checkout", "-q", head)  # detach
    with pytest.raises(wt.PreconditionError, match="detached"):
        wt.check_preconditions(tmp_path)


def test_repo_git_lock_serializes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    lock = wt.RepoGitLock(tmp_path)
    with lock.acquire():
        assert (tmp_path / ".imp" / "repo.lock").exists()
    with lock.acquire():  # re-acquire after release
        pass


def test_create_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = wt.create_worktree(tmp_path, "add-oauth", "main")
    assert path == tmp_path / ".imp" / "worktrees" / "add-oauth"
    assert path.is_dir()
    assert (path / "README.md").exists()  # checked out from main
    branches = _git(tmp_path, "branch", "--list", "imp/add-oauth").stdout
    assert "imp/add-oauth" in branches


def test_create_worktree_branch_collision(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "dup", "main")
    with pytest.raises(wt.BranchExists):
        wt.create_worktree(tmp_path, "dup", "main")


def test_find_control_root_from_inside_worktree(tmp_path: Path) -> None:
    # The resume path relies on resolving the control root from INSIDE a linked
    # worktree (spec §5.3) — not just a subdir of the main checkout.
    _init_repo(tmp_path)
    wpath = wt.create_worktree(tmp_path, "feat", "main")
    assert wt.find_control_root(wpath) == tmp_path.resolve()
    sub = wpath / ".imp" / "change"; sub.mkdir(parents=True)
    assert wt.find_control_root(sub) == tmp_path.resolve()


def test_remove_worktree_keeps_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = wt.create_worktree(tmp_path, "gone", "main")
    wt.remove_worktree(tmp_path, "gone")
    assert not path.exists()
    # Branch is KEPT (spec §4, §5 item 5).
    assert "imp/gone" in _git(tmp_path, "branch", "--list", "imp/gone").stdout
