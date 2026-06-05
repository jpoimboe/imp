from pathlib import Path

from imp.engine.state import State


def test_state_roundtrips_id_and_pending_rename(tmp_path: Path) -> None:
    s = State(id="2026-06-04-abc123", step="s", step_attempt=0, status="running")
    s.pending_rename = "rename-me"
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.id == "2026-06-04-abc123"
    assert loaded.pending_rename == "rename-me"


def test_merge_blocked_is_valid_status(tmp_path: Path) -> None:
    s = State(id="x", step=None, step_attempt=0, status="merge_blocked")
    s.save(tmp_path)
    assert State.load(tmp_path).status == "merge_blocked"
