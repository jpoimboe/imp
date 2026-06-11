from __future__ import annotations

import pytest

from imp.remote.drivers import telegram as imp_telegram


def test_send_message_params_shape() -> None:
    assert imp_telegram.send_message_params(5, "hi", silent=True) == {
        "chat_id": 5, "text": "hi", "disable_notification": True}


def test_get_updates_params_shape() -> None:
    assert imp_telegram.get_updates_params(3, 25) == {"offset": 3, "timeout": 25}


def test_make_send_calls_api_with_sendmessage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        imp_telegram, "api_call",
        lambda token, method, params: calls.append((token, method, params)))
    imp_telegram.make_send("TOK")(5, "hello", silent=False)
    assert calls == [(
        "TOK", "sendMessage",
        {"chat_id": 5, "text": "hello", "disable_notification": False})]


def test_make_get_updates_calls_api_with_getupdates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def fake_api(token, method, params):
        calls.append((token, method, params))
        return {"result": []}

    monkeypatch.setattr(imp_telegram, "api_call", fake_api)
    result = imp_telegram.make_get_updates("TOK")(4)
    assert result == {"result": []}
    assert calls == [(
        "TOK", "getUpdates",
        {"offset": 4, "timeout": imp_telegram._LONG_POLL_TIMEOUT})]
