from __future__ import annotations

import json
from pathlib import Path

from imp.cli.leave import (
    _had_exchange,
    _was_renamed,
    leave_control_action,
    resolve_leave_action,
)
from imp.engine.change import Change
from tests.conftest import _seed_control


class _Log:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


def _draft(tmp_path: Path, change_id: str, *, exchange: bool) -> Change:
    ch = Change.create_draft(
        tmp_path, title="D", change_id=change_id, base_branch="main")
    if exchange:
        ch.event_log().append(
            agent_id=None, type="user_message",
            payload={"role": "user", "content": "hi"})
    return ch


def test_chat_leave_with_exchange_archives(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "c1", exchange=True)
    assert leave_control_action(ch, "chat", None) == "archive"


def test_chat_leave_zero_exchange_discards(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "c2", exchange=False)
    assert leave_control_action(ch, "chat", None) == "discard"


def test_new_leave_with_exchange_saves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "n1", exchange=True)
    assert leave_control_action(ch, "new", None) == "save"


def test_new_leave_zero_exchange_discards(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "n2", exchange=False)
    assert leave_control_action(ch, "new", None) == "discard"


def test_save_intent_saves_even_with_zero_exchange(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "s1", exchange=False)
    assert leave_control_action(ch, "chat", "save") == "save"


def test_save_intent_with_no_leave_mode_still_saves(tmp_path: Path) -> None:
    # Explicit /save is exempt from the leave_mode-None early-return guard.
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "sn1", exchange=True)
    assert leave_control_action(ch, None, "save") == "save"


def test_clear_intent_with_exchange_archives_even_for_new_mode(tmp_path: Path) -> None:
    # /clear is chat-style: archive (not save) despite the cleared session's "new" mode.
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "cl1", exchange=True)
    assert leave_control_action(ch, "new", "clear") == "archive"


def test_clear_intent_zero_exchange_discards(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "cl2", exchange=False)
    assert leave_control_action(ch, "new", "clear") == "discard"


def test_abandon_intent_returns_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "a1", exchange=True)
    assert leave_control_action(ch, "chat", "abandon") is None


def test_no_leave_mode_no_intent_returns_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "h1", exchange=True)
    assert leave_control_action(ch, None, None) is None


def test_materialized_change_returns_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "mat1", exchange=True)
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    meta["materialized"] = True
    meta_path.write_text(json.dumps(meta))
    reloaded = Change.load(tmp_path, "mat1")
    assert leave_control_action(reloaded, "chat", None) is None


def test_resolve_leave_action_reloads_and_decides(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "r1", exchange=True)
    assert resolve_leave_action(tmp_path, "r1", "chat", None) == "archive"


def test_resolve_leave_action_returns_none_when_home_gone(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert resolve_leave_action(tmp_path, "ghost", "chat", None) is None


def test_had_exchange_false_for_only_system_events() -> None:
    log = _Log([
        {"type": "step_context", "payload": {}},
        {"type": "llm_response", "payload": {}},
        {"type": "awaiting_user_input", "payload": {}},
    ])
    assert _had_exchange(log) is False


def test_had_exchange_true_for_answered_awaiting_user_input() -> None:
    # Migrated shape: awaiting_user_input + user_message (no wfu tool_call/tool_result).
    log = _Log([
        {"type": "awaiting_user_input", "payload": {"prompt": ""}},
        {"type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
    ])
    assert _had_exchange(log) is True


def test_had_exchange_false_for_tool_results_only() -> None:
    # tool_call/tool_result events without any user-originated event → False.
    log = _Log([
        {"type": "tool_call",
         "payload": {"id": "g1", "name": "grep", "input": {}}},
        {"type": "tool_result",
         "payload": {"id": "g1", "content": "...", "error": False}},
    ])
    assert _had_exchange(log) is False


def test_had_exchange_true_for_a_typed_reply() -> None:
    assert _had_exchange(_Log([{"type": "user_message"}])) is True


def test_had_exchange_true_for_a_seed_opening_intent() -> None:
    assert _had_exchange(_Log([{"type": "opening_intent"}])) is True


def test_had_exchange_true_for_user_interjected() -> None:
    assert _had_exchange(_Log([{"type": "user_interjected"}])) is True


def test_renamed_chat_leave_saves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "rn1", exchange=True)
    ch.event_log().append(
        agent_id=None, type="change_renamed",
        payload={"id": "rn1", "old_title": "D", "new_title": "Real design"})
    assert leave_control_action(ch, "chat", None) == "save"


def test_renamed_chat_with_clear_still_archives(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "rn2", exchange=True)
    ch.event_log().append(
        agent_id=None, type="change_renamed",
        payload={"id": "rn2", "old_title": "D", "new_title": "Real design"})
    assert leave_control_action(ch, "chat", "clear") == "archive"


def test_was_renamed_true_for_change_renamed_event() -> None:
    assert _was_renamed(_Log([{"type": "change_renamed"}])) is True


def test_zero_exchange_renamed_chat_discards(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _draft(tmp_path, "rn3", exchange=False)
    ch.event_log().append(
        agent_id=None, type="change_renamed",
        payload={"id": "rn3", "old_title": "D", "new_title": "Real design"})
    assert leave_control_action(ch, "chat", None) == "discard"


def test_was_renamed_false_without_change_renamed_event() -> None:
    log = _Log([
        {"type": "opening_intent"},
        {"type": "user_message"},
    ])
    assert _was_renamed(log) is False
