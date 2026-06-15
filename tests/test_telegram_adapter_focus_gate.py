from __future__ import annotations

from imp.remote.daemon.focus import Focus
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter

A = "2026-06-14-aaaaaa"
B = "2026-06-14-bbbbbb"


def _adapter(focus: Focus, sent: list) -> TelegramNorthAdapter:
    return TelegramNorthAdapter(
        chat_id=1, submit=lambda command: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda offset: {"result": []}, focus=focus)


def _silent(change_id: str) -> dict:
    return {"type": "agent_output", "payload": {"text": "narrative"},
            "change": {"id": change_id}}


def _loud(change_id: str) -> dict:
    return {"type": "awaiting_user_input", "payload": {"prompt": "?"},
            "change": {"id": change_id}}


def test_silent_event_sent_when_change_matches_focus() -> None:
    focus = Focus()
    focus.set(A)
    sent: list = []
    _adapter(focus, sent).on_event(_silent(A))
    assert len(sent) == 1
    assert sent[0][1] is True   # silent


def test_silent_event_dropped_when_change_differs_from_focus() -> None:
    focus = Focus()
    focus.set(A)
    sent: list = []
    _adapter(focus, sent).on_event(_silent(B))
    assert sent == []


def test_silent_event_dropped_when_no_focus() -> None:
    focus = Focus()
    sent: list = []
    _adapter(focus, sent).on_event(_silent(A))
    assert sent == []


def test_loud_event_sent_regardless_of_focus() -> None:
    focus = Focus()
    focus.set(A)
    sent: list = []
    _adapter(focus, sent).on_event(_loud(B))
    assert len(sent) == 1
    assert sent[0][1] is False   # loud


def test_dropped_type_stays_dropped() -> None:
    focus = Focus()
    focus.set(A)
    sent: list = []
    _adapter(focus, sent).on_event(
        {"type": "thinking", "payload": {}, "change": {"id": A}})
    assert sent == []


def test_silent_event_with_empty_body_stays_dropped() -> None:
    focus = Focus()
    focus.set(A)
    sent: list = []
    _adapter(focus, sent).on_event(
        {"type": "agent_output", "payload": {"text": ""}, "change": {"id": A}})
    assert sent == []
