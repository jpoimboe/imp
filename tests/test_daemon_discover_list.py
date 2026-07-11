from __future__ import annotations

from pathlib import Path

from imp.engine.quest import Quest
from imp.engine.state import State
from imp.remote.daemon.discover import QuestInfo, list_quests
from tests.conftest import _seed_control


def test_list_quests_reports_id_title_status_step(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Add dark mode", quest_id="alpha", base_branch="main")
    infos = {c.id: c for c in list_quests(tmp_path)}
    assert set(infos) == {"alpha"}
    info = infos["alpha"]
    assert isinstance(info, QuestInfo)
    assert info.title == "Add dark mode"
    assert info.status == "new"          # Quest.create seeds status="new"
    assert info.step == "s"              # the seed workflow's single step name


def test_list_quests_reflects_a_paused_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="B", quest_id="beta", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    info = {c.id: c for c in list_quests(tmp_path)}["beta"]
    assert info.status == "paused"


def test_list_quests_empty_when_no_changes(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert list_quests(tmp_path) == []


def test_list_quests_excludes_terminal_changes(tmp_path: Path) -> None:
    # Completed / abandoned / merged Quests are terminal — a remote `/list`
    # should not surface them (only in-flight Quests a user can still act on).
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Live", quest_id="live", base_branch="main")
    for terminal in ("completed", "abandoned", "merged"):
        ch = Quest.create(
            tmp_path, title=terminal.title(), quest_id=terminal, base_branch="main")
        st = State.load(ch.state_dir)
        st.status = terminal
        st.save(ch.state_dir)
    ids = {c.id for c in list_quests(tmp_path)}
    assert ids == {live.id}   # only the in-flight (new) Quest survives the filter
