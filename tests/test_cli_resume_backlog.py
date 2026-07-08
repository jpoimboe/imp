from __future__ import annotations

from pathlib import Path

from imp.cli import main
from imp.engine.change import Change
from tests.conftest import _seed_control


def _user_message(ch: Change, text: str) -> None:
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"content": [{"type": "text", "text": text}]})


def test_resume_opens_fresh_true_for_fresh_idea_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Add OAuth", change_id="2026-06-16-idea22",
        base_branch="main", origin="idea", seed_opening="Add OAuth\n\nwire login")
    assert main._resume_opens_fresh(ch) is True


def test_resume_opens_fresh_false_for_unseeded_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Draft", change_id="2026-06-16-drf33",
        base_branch="main", origin="draft")
    _user_message(ch, "let's scope this")
    assert main._resume_opens_fresh(ch) is False


def test_resume_opens_fresh_false_for_materialized_change(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Live", change_id="live444", base_branch="main")
    assert main._resume_opens_fresh(ch) is False


def test_resume_opens_fresh_false_when_seeded_home_already_conversed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Idea", change_id="2026-06-16-idea55",
        base_branch="main", origin="idea", seed_opening="Idea")
    _user_message(ch, "more context")
    assert main._resume_opens_fresh(ch) is False


def test_warn_if_blocked_prints_for_unmerged_dep(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Blocked", change_id="2026-06-16-blk66",
        base_branch="main", origin="followup",
        depends_on=["2026-06-16-ghost1"], seed_opening="Blocked")
    main._warn_if_blocked(ch)
    err = capsys.readouterr().err
    assert "blocked on unmerged dep(s)" in err
    assert "2026-06-16-ghost1" in err
    assert "starting anyway" in err


def test_warn_if_blocked_silent_when_ready(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Ready", change_id="2026-06-16-rdy77",
        base_branch="main", origin="idea", seed_opening="Ready")
    main._warn_if_blocked(ch)
    assert capsys.readouterr().err == ""
