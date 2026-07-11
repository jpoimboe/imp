from __future__ import annotations

from pathlib import Path

from imp.cli import main
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


def test_resume_opens_fresh_true_for_fresh_idea_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Add OAuth", quest_id="2026-06-16-idea22",
        base_branch="main", origin="idea", seed_opening="Add OAuth\n\nwire login")
    assert main._resume_opens_fresh(ch) is True


def test_resume_opens_fresh_false_for_unseeded_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Draft", quest_id="2026-06-16-drf33",
        base_branch="main", origin="draft")
    assert main._resume_opens_fresh(ch) is False


def test_resume_opens_fresh_false_for_materialized_change(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="live444", base_branch="main")
    assert main._resume_opens_fresh(ch) is False


def test_resume_opens_fresh_false_when_seeded_home_already_conversed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Idea", quest_id="2026-06-16-idea55",
        base_branch="main", origin="idea", seed_opening="Idea")
    # "Has run" is the durable State.has_started fact now, not the presence of a
    # user_message event: a seeded draft that has run >=1 step must replay.
    st = State.load(ch.state_dir)
    st.has_started = True
    st.save(ch.state_dir)
    assert main._resume_opens_fresh(ch) is False


def test_warn_if_blocked_prints_for_unmerged_dep(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Blocked", quest_id="2026-06-16-blk66",
        base_branch="main", origin="followup",
        depends_on=["2026-06-16-ghost1"], seed_opening="Blocked")
    main._warn_if_blocked(ch)
    err = capsys.readouterr().err
    assert "blocked on unmerged dep(s)" in err
    assert "2026-06-16-ghost1" in err
    assert "starting anyway" in err


def test_warn_if_blocked_silent_when_ready(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Ready", quest_id="2026-06-16-rdy77",
        base_branch="main", origin="idea", seed_opening="Ready")
    main._warn_if_blocked(ch)
    assert capsys.readouterr().err == ""
