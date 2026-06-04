from pathlib import Path

from imp.engine import worktree
from imp.engine.change import Change
from tests.conftest import _git, _seed_control


def test_remove_worktree_force_tolerates_dirty_tree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    (ch.project_root / "dirty.txt").write_text("uncommitted")  # plain remove would error
    worktree.remove_worktree(tmp_path, "feat", force=True)
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()


def test_remove_worktree_force_tolerates_absent_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    worktree.remove_worktree(tmp_path, "feat", force=True)
    # Second call: worktree already gone + stale metadata; must not raise.
    worktree.remove_worktree(tmp_path, "feat", force=True)


def test_delete_branch_removes_and_is_idempotent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    worktree.remove_worktree(tmp_path, "feat", force=True)  # branch must be uncheckedout first
    worktree.delete_branch(tmp_path, "feat")
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""
    # Idempotent: deleting an already-deleted branch does not raise.
    worktree.delete_branch(tmp_path, "feat")
