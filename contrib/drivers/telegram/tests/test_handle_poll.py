# contrib/drivers/telegram/tests/test_handle_poll.py
from __future__ import annotations

import imp_telegram


def test_handle_event_sends_loud_with_notification() -> None:
    sent: list[tuple] = []

    def send(chat_id, text, *, silent):
        sent.append((chat_id, text, silent))

    handled = imp_telegram.handle_event(
        {"type": "awaiting_user_input", "change": {"title": "T"}, "payload": {}},
        send, chat_id=5)
    assert handled is True
    assert len(sent) == 1
    chat_id, text, silent = sent[0]
    assert chat_id == 5
    assert silent is False                     # loud → notify
    assert text == "[T] awaiting_user_input"


def test_handle_event_sends_silent_narrative() -> None:
    sent: list[tuple] = []
    imp_telegram.handle_event(
        {"type": "agent_output", "change": {"title": "T"}, "payload": {"text": "hi"}},
        lambda chat_id, text, *, silent: sent.append(silent), chat_id=5)
    assert sent == [True]                       # silent → disable_notification


def test_handle_event_drops_uncurated_without_sending() -> None:
    sent: list[tuple] = []
    handled = imp_telegram.handle_event(
        {"type": "tool_call", "change": {}, "payload": {}},
        lambda *a, **k: sent.append(1), chat_id=5)
    assert handled is False
    assert sent == []                           # dropped → no Telegram call


def test_poll_once_emits_allowlisted_replies_and_returns_advanced_offset() -> None:
    response = {"result": [
        {"update_id": 20, "message": {"chat": {"id": 5}, "text": "go"}},
        {"update_id": 21, "message": {"chat": {"id": 999}, "text": "ignored"}},
    ]}
    written: list[str] = []
    new_offset = imp_telegram.poll_once(
        lambda offset: response, chat_id=5, offset=0, write_reply=written.append)
    assert written == ["go"]                    # foreign chat filtered out
    assert new_offset == 22                      # offset advanced past both updates
