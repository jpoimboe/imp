from __future__ import annotations

import inspect
from typing import Any

from imp.remote.daemon import telegram_adapter
from imp.remote.daemon.telegram_adapter import format_outbound, send_message

EVENT = {
    "type": "awaiting_user_input",
    "payload": {"steer_hint": "Need your call"},
    "quest": {"id": "2026-06-13-k3f9qa", "title": "Dark mode"},
}


def test_format_outbound_carries_body_and_handle() -> None:
    # The gate body is the humanized label alone (no '[title]' bracket — identity
    # rides the header_line); the reply handle is still appended for the gate.
    assert format_outbound(EVENT, "k3f9qa") == (
        "needs your input\nNeed your call\n↩ @k3f9qa")


def test_format_outbound_omits_handle_for_non_await_event() -> None:
    # Every non-gate event renders with NO handle line.
    event = {"type": "quest_merged", "payload": {},
             "quest": {"id": "k3f9qa", "title": "Dark mode"}}
    assert format_outbound(event, "k3f9qa") == "done"


def test_send_message_returns_the_new_message_id(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        captured["method"] = method
        captured["params"] = params
        return {"ok": True, "result": {"message_id": 777}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    mid = send_message("tok", 4242, "hello", silent=False)
    assert mid == 777
    assert captured["method"] == "sendMessage"
    assert captured["params"]["chat_id"] == 4242
    assert captured["params"]["text"] == "hello"
    assert captured["params"]["disable_notification"] is False


def test_send_message_returns_none_when_response_has_no_message_id(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        telegram_adapter, "api_call", lambda *a, **k: {"ok": False})
    assert send_message("tok", 4242, "hello", silent=True) is None


def test_send_message_splits_overlong_text_into_ordered_sends(
    monkeypatch: Any,
) -> None:
    calls: list[dict] = []

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        calls.append(params)
        return {"ok": True, "result": {"message_id": len(calls)}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    body = "a" * 5000
    send_message("tok", 4242, body, silent=False)
    assert len(calls) == 2
    assert all(len(c["text"]) <= 4096 for c in calls)
    assert "".join(c["text"] for c in calls) == body
    assert all("[truncated]" not in c["text"] for c in calls)
    assert all(c["disable_notification"] is False for c in calls)


def test_send_message_leaves_short_text_unchanged(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        captured["params"] = params
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    send_message("tok", 4242, "short", silent=False)
    assert captured["params"]["text"] == "short"


def test_send_message_returns_last_chunk_message_id(monkeypatch: Any) -> None:
    ids = iter([101, 202])

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        return {"ok": True, "result": {"message_id": next(ids)}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    # A 5000-char body splits into two sends; the returned id is the LAST send's.
    assert send_message("tok", 4242, "a" * 5000, silent=True) == 202


def test_send_message_returns_none_when_final_chunk_has_no_id(
    monkeypatch: Any,
) -> None:
    responses = iter([
        {"ok": True, "result": {"message_id": 101}},
        {"ok": False},
    ])

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        return next(responses)

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    # Final chunk yields no message id → None, even though an earlier chunk had one.
    assert send_message("tok", 4242, "a" * 5000, silent=True) is None


def test_format_outbound_signature_has_no_body_override() -> None:
    params = list(inspect.signature(format_outbound).parameters)
    assert params == ["event", "handle"]


def test_adapter_no_longer_imports_compose_message() -> None:
    assert not hasattr(telegram_adapter, "_compose_message")
