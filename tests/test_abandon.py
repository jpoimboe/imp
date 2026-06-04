from pathlib import Path

from imp.cli.main import _abandon, _completed_rows, _status_rows
from imp.engine import journal, worktree
from imp.engine.change import Change
from imp.engine.state import ProjectLock, State
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


def test_abandon_happy_path_tears_down_and_journals(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="x", slug="feat", base_branch="main")

    rc = _abandon(tmp_path, "feat")
    assert rc == 0
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()       # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # branch gone

    # Durable: change_abandoned event + abandoned status retrievable via journal.
    state_json = journal.journal_show(tmp_path, "feat", "state.json")
    assert state_json is not None and b'"abandoned"' in state_json
    events = journal.journal_show(tmp_path, "feat", "events.jsonl")
    assert events is not None and b"change_abandoned" in events


def test_abandon_surfaces_as_completed_not_inflight(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    _abandon(tmp_path, "feat")

    assert all(r[0] != "feat" for r in _status_rows(tmp_path))           # not in-flight
    completed = {r[0]: r[1] for r in _completed_rows(tmp_path)}
    assert completed.get("feat") == "abandoned"                          # labeled abandoned


def test_abandon_allowed_from_completed_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "completed"          # completed-but-not-merged: abandon is the only verb
    st.save(ch.state_dir)
    assert _abandon(tmp_path, "feat") == 0
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()


def test_abandon_refuses_when_lock_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    with ProjectLock(ch.change_dir / "lock").acquire():     # simulate a live runner
        rc = _abandon(tmp_path, "feat")
    assert rc == 2
    assert (tmp_path / ".imp" / "worktrees" / "feat").exists()  # untouched


def test_abandon_already_gone_is_idempotent_and_reaps_branch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    # Simulate a crash between (d) worktree removal and (e) branch deletion:
    # worktree gone, branch orphaned.
    worktree.remove_worktree(tmp_path, "feat", force=True)
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() != ""  # orphan exists

    rc = _abandon(tmp_path, "feat")
    assert rc == 0                                              # not the rc=2 LoadError path
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # reaped
