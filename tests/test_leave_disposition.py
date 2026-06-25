from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.abandon import discard_conversation, save_to_backlog
from imp.engine.archive import record, records_root
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.state import LockHeld, ProjectLock, State
from tests.conftest import _seed_control


def test_save_to_backlog_sets_paused_status_and_keeps_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Draft", change_id="d1", base_branch="main")
    assert State.load(ch.state_dir).status == "new"

    save_to_backlog(ch)

    assert State.load(ch.state_dir).status == "paused"            # resting Backlog state
    assert (tmp_path / ".imp" / "changes" / "d1").exists()       # live home KEPT
    assert record.read_record(records_root(tmp_path), "d1") is None  # NOT archived
    events = ch.event_log().read()
    assert any(e.get("type") == "conversation_saved" for e in events)


def test_save_to_backlog_raises_lock_held_when_lock_is_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Draft", change_id="d3", base_branch="main")
    with ProjectLock(ch.change_dir / "lock").acquire(), pytest.raises(LockHeld):
        save_to_backlog(ch)


def test_discard_conversation_raises_lock_held_when_lock_is_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Draft", change_id="d4", base_branch="main")
    with ProjectLock(ch.change_dir / "lock").acquire(), pytest.raises(LockHeld):
        discard_conversation(ch)


def test_discard_conversation_drops_home_and_leaves_no_record(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Draft", change_id="d2", base_branch="main")

    discard_conversation(ch)

    assert not (tmp_path / ".imp" / "changes" / "d2").exists()   # home gone
    assert record.read_record(records_root(tmp_path), "d2") is None  # zero-exchange → no record


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Log:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


def test_had_exchange_false_for_only_system_events() -> None:
    log = _Log([
        {"type": "step_context", "payload": {}},
        {"type": "llm_response", "payload": {}},
        {"type": "tool_call",
         "payload": {"id": "w1", "name": "wait_for_user", "input": {}}},
        {"type": "awaiting_user_input", "payload": {}},
    ])
    assert m._had_exchange(log) is False


def test_had_exchange_true_for_an_answered_wait_for_user() -> None:
    log = _Log([
        {"type": "tool_call",
         "payload": {"id": "w1", "name": "wait_for_user", "input": {}}},
        {"type": "tool_result",
         "payload": {"id": "w1", "content": "tell me more", "error": False}},
    ])
    assert m._had_exchange(log) is True


def test_had_exchange_false_for_an_unrelated_tool_result() -> None:
    log = _Log([
        {"type": "tool_call",
         "payload": {"id": "g1", "name": "grep", "input": {}}},
        {"type": "tool_result",
         "payload": {"id": "g1", "content": "...", "error": False}},
        {"type": "tool_call",
         "payload": {"id": "w1", "name": "wait_for_user", "input": {}}},
    ])
    assert m._had_exchange(log) is False


def test_had_exchange_true_for_a_typed_reply() -> None:
    assert m._had_exchange(_Log([{"type": "user_message"}])) is True


def test_had_exchange_true_for_a_seed_opening_intent() -> None:
    assert m._had_exchange(_Log([{"type": "opening_intent"}])) is True


def _draft_with_exchange(tmp_path: Path, change_id: str) -> Change:
    ch = Change.create_draft(
        tmp_path, title="Draft", change_id=change_id, base_branch="main")
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    return ch


@pytest.mark.anyio
async def test_disposition_chat_leave_archives_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "c1")
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {})
    assert rc == 0
    assert not (tmp_path / ".imp" / "changes" / "c1").exists()     # archived, home dropped
    rec = record.read_record(records_root(tmp_path), "c1")
    assert rec is not None and rec["resumable"] is True


@pytest.mark.anyio
async def test_disposition_new_leave_saves_to_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "n1")
    rc = await m._maybe_apply_leave_disposition(ch, "new", {})
    assert rc == 0
    assert State.load(ch.state_dir).status == "paused"             # Backlog, home kept
    assert (tmp_path / ".imp" / "changes" / "n1").exists()


@pytest.mark.anyio
async def test_disposition_save_intent_overrides_chat_mode(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "s1")
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {"intent": "save"})
    assert rc == 0
    assert State.load(ch.state_dir).status == "paused"             # /save → Backlog, not archive
    assert (tmp_path / ".imp" / "changes" / "s1").exists()


@pytest.mark.anyio
async def test_disposition_zero_exchange_leaves_no_record(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="", change_id="z1", base_branch="main")
    ch.event_log().append(
        agent_id="front-door", type="tool_call",
        payload={"id": "w1", "name": "wait_for_user", "input": {}})
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {})
    assert rc == 0
    assert not (tmp_path / ".imp" / "changes" / "z1").exists()     # discarded
    assert record.read_record(records_root(tmp_path), "z1") is None


@pytest.mark.anyio
async def test_disposition_explicit_save_overrides_zero_exchange(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="", change_id="es1", base_branch="main")
    ch.event_log().append(
        agent_id="front-door", type="tool_call",
        payload={"id": "g1", "name": "wait_for_user", "input": {}})
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {"intent": "save"})
    assert rc == 0
    assert State.load(ch.state_dir).status == "paused"             # saved, NOT discarded
    assert (tmp_path / ".imp" / "changes" / "es1").exists()


@pytest.mark.anyio
async def test_disposition_saves_a_live_wait_for_user_conversation(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Draft", change_id="w1", base_branch="main")
    ch.event_log().append(
        agent_id="front-door", type="tool_call",
        payload={"id": "g1", "name": "wait_for_user", "input": {}})
    ch.event_log().append(
        agent_id="front-door", type="tool_result",
        payload={"id": "g1", "content": "add a dark mode toggle", "error": False})
    rc = await m._maybe_apply_leave_disposition(ch, "new", {})
    assert rc == 0
    assert State.load(ch.state_dir).status == "paused"             # saved, NOT discarded
    assert (tmp_path / ".imp" / "changes" / "w1").exists()


@pytest.mark.anyio
async def test_disposition_skips_abandon_intent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "a1")
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {"intent": "abandon"})
    assert rc is None
    assert (tmp_path / ".imp" / "changes" / "a1").exists()         # untouched


@pytest.mark.anyio
async def test_disposition_none_for_no_leave_mode(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "h1")
    assert await m._maybe_apply_leave_disposition(ch, None, {}) is None


@pytest.mark.anyio
async def test_disposition_returns_none_for_materialized_change(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "mat1")
    # Mark the change as materialized (phase 1+), simulating a real in-flight change
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    meta["materialized"] = True
    meta_path.write_text(json.dumps(meta))
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {})
    assert rc is None
    assert (tmp_path / ".imp" / "changes" / "mat1").exists()  # untouched


def test_had_exchange_true_for_user_interjected() -> None:
    assert m._had_exchange(_Log([{"type": "user_interjected"}])) is True


@pytest.mark.anyio
async def test_disposition_abandon_prints_prebuild_recover_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Dark mode", change_id="ab1", base_branch="main")
    rc = await m._maybe_apply_leave_disposition(ch, None, {"intent": "abandon"})
    assert rc is None
    out = capsys.readouterr().out
    assert "'Dark mode' abandoned" in out
    assert "imp resume ab1" in out
    assert "torn down" not in out


@pytest.mark.anyio
async def test_disposition_abandon_prints_materialized_torndown_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Big feature", change_id="ab2", base_branch="main")
    rc = await m._maybe_apply_leave_disposition(
        ch, None, {"intent": "abandon", "materialized": "1"})
    assert rc is None
    out = capsys.readouterr().out
    assert "'Big feature' abandoned" in out
    assert "torn down" in out
    assert "imp search" in out


@pytest.mark.anyio
async def test_disposition_new_leave_zero_exchange_discards(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="", change_id="zn1", base_branch="main")
    # No events — genuine zero-exchange
    rc = await m._maybe_apply_leave_disposition(ch, "new", {})
    assert rc == 0
    assert not (tmp_path / ".imp" / "changes" / "zn1").exists()
    assert record.read_record(records_root(tmp_path), "zn1") is None


@pytest.mark.anyio
async def test_apply_disposition_locked_raises_on_stuck_lock(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Draft", change_id="lh1", base_branch="main")
    lock = ProjectLock(ch.change_dir / "lock")
    with lock.acquire(), pytest.raises(LockHeld):
        await m._apply_disposition_locked(
            save_to_backlog, ch, attempts=1, delay=0.0)


@pytest.mark.anyio
async def test_disposition_lock_held_after_retries_prints_busy_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "lh2")

    async def _always_lock_held(fn, change, **kwargs):
        raise LockHeld(Path("/fake/lock"), None)

    monkeypatch.setattr(m, "_apply_disposition_locked", _always_lock_held)
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {})
    assert rc is None
    captured = capsys.readouterr()
    assert "still busy" in captured.err


@pytest.mark.anyio
async def test_disposition_returns_none_when_reload_raises_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "le1")

    def _raise(*args, **kwargs):
        raise LoadError("home gone")

    monkeypatch.setattr(m.Change, "load", _raise)
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {})
    assert rc is None


@pytest.mark.anyio
async def test_disposition_returns_none_when_reload_raises_load_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "le2")

    def _raise(*args, **kwargs):
        raise LoadErrors(errors=[LoadError("missing field")], warnings=[])

    monkeypatch.setattr(m.Change, "load", _raise)
    rc = await m._maybe_apply_leave_disposition(ch, "chat", {})
    assert rc is None


@pytest.mark.anyio
async def test_disposition_save_intent_honored_when_leave_mode_none(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = _draft_with_exchange(tmp_path, "rs1")
    rc = await m._maybe_apply_leave_disposition(ch, None, {"intent": "save"})
    assert rc == 0
    assert State.load(ch.state_dir).status == "paused"          # saved to Backlog
    assert (tmp_path / ".imp" / "changes" / "rs1").exists()     # home kept
