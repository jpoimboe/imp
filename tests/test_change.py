# tests/test_change.py
from pathlib import Path

from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def test_create_change_makes_worktree_and_state(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Add OAuth",
                       change_id="2026-06-04-abc123", base_branch="main")
    assert ch.id == "2026-06-04-abc123"
    assert ch.slug == "2026-06-04-add-oauth"     # derived from title + id's date
    assert ch.base_branch == "main"
    wtpath = tmp_path / ".imp" / "worktrees" / "2026-06-04-abc123"
    assert ch.project_root == wtpath
    assert (wtpath / ".imp" / "change" / "change.json").exists()
    assert (wtpath / ".imp" / "change" / "workflow" / "workflow.yaml").exists()
    state = State.load(wtpath / ".imp" / "change" / "state")
    assert state.id == "2026-06-04-abc123" and state.step == "s" and state.status == "running"


def test_load_change_by_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="X", change_id="2026-06-04-mychng", base_branch="main")
    b = Change.load(tmp_path, "2026-06-04-mychng")
    assert b.id == "2026-06-04-mychng" and b.title == "X" and b.base_branch == "main"


def test_change_json_has_no_slug_or_auto_titled(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="2026-06-04-zzz999", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert set(meta) == {"id", "title", "base_branch", "created_at"}


def test_snapshot_is_isolated(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="2026-06-04-isolat", base_branch="main")
    (tmp_path / "imp" / "workflows" / "default" / "workflow.yaml").write_text("BROKEN")
    assert "phases:" in (ch.workflow_dir / "workflow.yaml").read_text()
