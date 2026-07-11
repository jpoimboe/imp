from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.abandon import discard_conversation, save_to_backlog
from imp.engine.archive import record, records_root
from imp.engine.quest import Quest
from imp.engine.state import LockHeld, ProjectLock, State
from tests.conftest import _seed_control


def test_save_to_backlog_sets_paused_status_and_keeps_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="d1", base_branch="main")
    assert State.load(ch.state_dir).status == "new"

    save_to_backlog(ch)

    assert State.load(ch.state_dir).status == "paused"            # resting Backlog state
    assert (tmp_path / ".imp" / "quests" / "d1").exists()       # live home KEPT
    assert record.read_record(records_root(tmp_path), "d1") is None  # NOT archived
    events = ch.event_log().read()
    assert any(e.get("type") == "conversation_saved" for e in events)


def test_save_to_backlog_raises_lock_held_when_lock_is_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="d3", base_branch="main")
    with ProjectLock(ch.quest_dir / "lock").acquire(), pytest.raises(LockHeld):
        save_to_backlog(ch)


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


def test_disposition_abandon_draft_prints_honest_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Dark mode", quest_id="ab1", base_branch="main")
    rc = m._print_leave_disposition(ch, {"intent": "abandon"})
    assert rc is None
    out = capsys.readouterr().out
    assert out.strip() == "imp: 'Dark mode' abandoned"
    assert "imp resume" not in out
    assert "imp search" not in out
    assert "torn down" not in out


def test_disposition_abandon_prints_materialized_torndown_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="Big feature", quest_id="ab2", base_branch="main")
    rc = m._print_leave_disposition(ch, {"intent": "abandon", "materialized": "1"})
    assert rc is None
    out = capsys.readouterr().out
    assert "'Big feature' abandoned" in out
    assert "torn down" in out
    assert "imp search" in out


def test_print_leave_disposition_archive_is_lock_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "fast1")
    # The daemon is "still tearing down": it holds the per-change lock. The
    # old foreground retried ~10s on this lock; the new path must not touch it.
    with ProjectLock(ch.quest_dir / "lock").acquire():
        rc = m._print_leave_disposition(ch, {"disposition": "archive"})
    assert rc == 0
    # The foreground did NOT archive — the daemon owns the disposition.
    assert (tmp_path / ".imp" / "quests" / "fast1").exists()
    assert record.read_record(records_root(tmp_path), "fast1") is None
    out = capsys.readouterr().out
    assert "conversation saved" in out
    assert "imp resume fast1" in out


def test_print_leave_disposition_save_prints_backlog_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "bk1")
    rc = m._print_leave_disposition(ch, {"disposition": "save"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "to your Backlog" in out
    assert "imp resume bk1" in out


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


def test_print_leave_disposition_send_failed_prints_running_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "sf1")
    rc = m._print_leave_disposition(ch, {"send_failed": "1"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "left running" in out
    assert "imp resume sf1" in out


def test_save_to_backlog_arms_resume_pending(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Draft", quest_id="bkarm", base_branch="main")
    initial = State.load(ch.state_dir)
    step = initial.step
    assert step is not None
    assert initial.resume_pending is None                      # fresh: not yet armed

    save_to_backlog(ch)

    reloaded = State.load(ch.state_dir)                         # home kept, reload in place
    assert reloaded.resume_pending == step                     # armed to the step on leave
    assert reloaded.interrupt_pending is False                 # clean yield, not an interrupt
