from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.abandon import discard_conversation, save_as_draft
from imp.engine.archive import record, records_root
from imp.engine.quest import Quest
from imp.engine.state import LockHeld, ProjectLock, State
from tests.conftest import _seed_control


def test_save_as_draft_sets_paused_status_and_keeps_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="d1", base_branch="main")
    assert State.load(ch.state_dir).status == "new"

    save_as_draft(ch)

    assert State.load(ch.state_dir).status == "paused"            # resting draft state
    assert (tmp_path / ".imp" / "quests" / "d1").exists()       # live home KEPT
    assert record.read_record(records_root(tmp_path), "d1") is None  # NOT archived
    events = ch.event_log().read()
    assert any(e.get("type") == "conversation_saved" for e in events)


def test_save_as_draft_raises_lock_held_when_lock_is_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="d3", base_branch="main")
    with ProjectLock(ch.quest_dir / "lock").acquire(), pytest.raises(LockHeld):
        save_as_draft(ch)


def test_discard_conversation_raises_lock_held_when_lock_is_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="d4", base_branch="main")
    with ProjectLock(ch.quest_dir / "lock").acquire(), pytest.raises(LockHeld):
        discard_conversation(ch)


def test_discard_conversation_drops_home_and_leaves_no_record(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="d2", base_branch="main")

    discard_conversation(ch)

    assert not (tmp_path / ".imp" / "quests" / "d2").exists()   # home gone
    assert record.read_record(records_root(tmp_path), "d2") is None  # zero-exchange → no record


def _draft_with_exchange(tmp_path: Path, quest_id: str) -> Quest:
    ch = Quest.create_draft(
        tmp_path, title="Draft", quest_id=quest_id, base_branch="main")
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    return ch


def test_disposition_abandon_prints_spine_no_torn_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Dark mode", quest_id="ab1", base_branch="main")
    rc = m._print_leave_disposition(ch, {"intent": "abandon"})
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "ab1 · Dark mode · abandoned\n\n"
    assert "torn down" not in out
    assert "imp resume" not in out
    assert "imp search" not in out


def test_disposition_abandon_materialized_also_has_no_torn_down_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Big feature", quest_id="ab2", base_branch="main")
    rc = m._print_leave_disposition(ch, {"intent": "abandon", "materialized": "1"})
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "ab2 · Big feature · abandoned\n\n"
    assert "torn down" not in out
    assert "imp search" not in out


def test_print_leave_disposition_archive_is_lock_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "fast1")
    with ProjectLock(ch.quest_dir / "lock").acquire():
        rc = m._print_leave_disposition(ch, {"disposition": "archive"})
    assert rc == 0
    assert (tmp_path / ".imp" / "quests" / "fast1").exists()
    assert record.read_record(records_root(tmp_path), "fast1") is None
    out = capsys.readouterr().out
    assert "· shelved" in out
    assert "resume: imp resume fast1 — or find it later with imp search" in out
    assert not out.startswith("\n")
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_print_leave_disposition_save_prints_kept_as_draft_spine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "bk1")
    rc = m._print_leave_disposition(ch, {"disposition": "save"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "· kept as draft" in out
    assert "saved to backlog" not in out
    assert "resume: imp resume bk1" in out
    assert not out.startswith("\n")
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_print_leave_disposition_discard_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "dx1")
    rc = m._print_leave_disposition(ch, {"disposition": "discard"})
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_print_leave_disposition_none_without_recorded_disposition(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "no1")
    assert m._print_leave_disposition(ch, {}) is None


def test_print_leave_disposition_send_failed_prints_running_spine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "sf1")
    rc = m._print_leave_disposition(ch, {"send_failed": "1"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "· left running" in out
    assert "resume: imp resume sf1" in out
    assert not out.startswith("\n")
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_save_as_draft_arms_resume_pending(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="bkarm", base_branch="main")
    initial = State.load(ch.state_dir)
    step = initial.step
    assert step is not None
    assert initial.resume_pending is None                      # fresh: not yet armed

    save_as_draft(ch)

    reloaded = State.load(ch.state_dir)                         # home kept, reload in place
    assert reloaded.resume_pending == step                     # armed to the step on leave
    assert reloaded.resume_mode == "solicit"                   # clean yield re-solicits


def test_save_as_draft_stamps_interrupt_reason_when_reason_given(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="bkcd", base_branch="main")

    save_as_draft(ch, reason="ctrl-d")

    saved = [e for e in ch.event_log().read() if e.get("type") == "conversation_saved"]
    assert len(saved) == 1
    assert saved[0]["payload"] == {"interrupt_reason": "ctrl-d"}


def test_save_as_draft_without_reason_carries_no_interrupt_reason(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="bknr", base_branch="main")

    save_as_draft(ch)

    saved = [e for e in ch.event_log().read() if e.get("type") == "conversation_saved"]
    assert len(saved) == 1
    assert "interrupt_reason" not in saved[0]["payload"]
    assert saved[0]["payload"] == {}


def _titleless_draft_with_intent(tmp_path: Path, quest_id: str, text: str) -> Quest:
    ch = Quest.create_draft(
        tmp_path, title="", quest_id=quest_id, base_branch="main")
    ch.event_log().append(
        agent_id=None, type="opening_intent", payload={"text": text})
    return ch


def test_leave_disposition_titleless_shelved_shows_prompt_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _titleless_draft_with_intent(tmp_path, "tl1", "when I hit save it crashed")
    rc = m._print_leave_disposition(ch, {"disposition": "archive"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "tl1 · > when I hit save it crashed · shelved" in out
    assert "(unnamed)" not in out


def test_leave_disposition_titleless_abandon_shows_prompt_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _titleless_draft_with_intent(tmp_path, "tl3", "make dark mode work")
    rc = m._print_leave_disposition(ch, {"intent": "abandon"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "tl3 · > make dark mode work · abandoned" in out
    assert "(unnamed)" not in out


def test_leave_disposition_titleless_left_running_shows_prompt_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _titleless_draft_with_intent(tmp_path, "tl4", "fix the login crash")
    rc = m._print_leave_disposition(ch, {"send_failed": "1"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "tl4 · > fix the login crash · left running" in out
    assert "(unnamed)" not in out


def test_leave_disposition_titleless_no_prompt_shows_unnamed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="", quest_id="tl2", base_branch="main")
    rc = m._print_leave_disposition(ch, {"disposition": "save"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "tl2 · (unnamed) · kept as draft" in out
    assert "> (unnamed)" not in out
