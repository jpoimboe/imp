from __future__ import annotations

from typing import Any

from imp.remote.daemon import telegram_adapter
from imp.remote.daemon.telegram_adapter import format_outbound, send_message

EVENT = {
    "type": "awaiting_user_input",
    "payload": {"prompt": "Need your call"},
    "change": {"id": "2026-06-13-k3f9qa", "title": "Dark mode"},
}


def test_format_outbound_carries_body_and_handle() -> None:
    # The gate body is the humanized label alone (no '[title]' bracket — identity
    # rides the header_line); the reply handle is still appended for the gate.
    assert format_outbound(EVENT, "k3f9qa") == (
        "needs your input\nNeed your call\n↩ @k3f9qa")


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


def test_send_message_clamps_overlong_text(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        captured["params"] = params
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    send_message("tok", 4242, "a" * 5000, silent=False)
    sent = captured["params"]["text"]
    assert len(sent) == 4096
    assert sent.endswith("\n[truncated]")


def test_send_message_leaves_short_text_unchanged(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        captured["params"] = params
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    send_message("tok", 4242, "short", silent=False)
    assert captured["params"]["text"] == "short"
