# tests/test_change.py
from pathlib import Path

from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def test_create_change_makes_worktree_and_state(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Add OAuth", slug="add-oauth", base_branch="main")
    assert ch.slug == "add-oauth"
    assert ch.base_branch == "main"
    wtpath = tmp_path / ".imp" / "worktrees" / "add-oauth"
    assert ch.project_root == wtpath
    assert (wtpath / ".imp" / "change" / "change.json").exists()
    # No snapshot copy: the gitignored .imp/change/workflow/ is gone.
    assert not (wtpath / ".imp" / "change" / "workflow").exists()
    # workflow_dir IS the branch's own tracked workflow dir.
    assert ch.workflow_dir == wtpath / "imp" / "workflows" / "default"
    assert (ch.workflow_dir / "workflow.yaml").exists()
    state = State.load(wtpath / ".imp" / "change" / "state")
    assert state.slug == "add-oauth" and state.step == "s" and state.status == "running"


def test_load_change_by_slug(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="X", slug="my-change", base_branch="main")
    b = Change.load(tmp_path, "my-change")
    assert b.slug == "my-change" and b.title == "X" and b.base_branch == "main"


def test_workflow_dir_is_the_branch_dir_not_a_copy(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", slug="iso", base_branch="main")
    # The executed workflow lives in the worktree's tracked tree...
    assert ch.workflow_dir == ch.project_root / "imp" / "workflows" / "default"
    assert "phases:" in (ch.workflow_dir / "workflow.yaml").read_text()
    # ...and there is no gitignored second copy.
    assert not (ch.change_dir / "workflow").exists()
