from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from imp.engine.state import State
from imp.remote.daemon.discover import ChangeInfo, list_changes
from tests.conftest import _seed_control


def test_list_changes_reports_id_title_status_step(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Add dark mode", change_id="alpha", base_branch="main")
    infos = {c.id: c for c in list_changes(tmp_path)}
    assert set(infos) == {"alpha"}
    info = infos["alpha"]
    assert isinstance(info, ChangeInfo)
    assert info.title == "Add dark mode"
    assert info.status == "running"      # Change.create seeds status="running"
    assert info.step == "s"              # the seed workflow's single step name


def test_list_changes_reflects_a_paused_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    info = {c.id: c for c in list_changes(tmp_path)}["beta"]
    assert info.status == "paused"


def test_list_changes_empty_when_no_changes(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert list_changes(tmp_path) == []


def test_list_changes_excludes_terminal_changes(tmp_path: Path) -> None:
    # Completed / abandoned / merged Changes are terminal — a remote `/list`
    # should not surface them (only in-flight Changes a user can still act on).
    _seed_control(tmp_path)
    live = Change.create(tmp_path, title="Live", change_id="live", base_branch="main")
    for terminal in ("completed", "abandoned", "merged"):
        ch = Change.create(
            tmp_path, title=terminal.title(), change_id=terminal, base_branch="main")
        st = State.load(ch.state_dir)
        st.status = terminal
        st.save(ch.state_dir)
    ids = {c.id for c in list_changes(tmp_path)}
    assert ids == {live.id}   # only the running Change survives the filter
