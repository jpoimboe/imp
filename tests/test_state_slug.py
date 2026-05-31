from pathlib import Path

from imp.engine.state import State


def test_state_roundtrips_slug_and_pending_rename(tmp_path: Path) -> None:
    s = State(slug="add-oauth", step="s", step_attempt=0, status="running")
    s.pending_rename = "rename-me"
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.slug == "add-oauth"
    assert loaded.pending_rename == "rename-me"


def test_merge_blocked_is_valid_status(tmp_path: Path) -> None:
    s = State(slug="x", step=None, step_attempt=0, status="merge_blocked")
    s.save(tmp_path)
    assert State.load(tmp_path).status == "merge_blocked"
