from __future__ import annotations

import io

import pytest

from imp.remote.drivers import telegram as imp_telegram


def test_main_refuses_to_start_without_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Refusal must happen before any network call (spec AC 11).
    def boom(*a, **k):
        raise AssertionError("api_call must not run when config is invalid")

    monkeypatch.setattr(imp_telegram, "api_call", boom)
    rc = imp_telegram.main(env={"IMP_TELEGRAM_TOKEN": "tok"}, stdin=io.StringIO(""))
    assert rc == 2


def test_main_routes_a_stdin_event_to_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple] = []
    get_offsets: list[int] = []

    monkeypatch.setattr(imp_telegram, "make_send",
                        lambda token: (lambda chat_id, text, *, silent: sent.append(
                            (chat_id, text, silent))))

    def fake_get_updates_factory(token):
        def get(offset):
            get_offsets.append(offset)
            return {"result": []}
        return get

    monkeypatch.setattr(imp_telegram, "make_get_updates", fake_get_updates_factory)
    # The startup self-check makes its own network calls; stub it out here so this
    # test exercises only the stdin→Telegram relay path (self-check is tested apart).
    monkeypatch.setattr(imp_telegram, "self_check", lambda *a, **k: None)

    # Stub the poll thread so the test stays deterministic (no background loop).
    class _NoThread:
        def __init__(self, *a, **k):
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(imp_telegram.threading, "Thread", _NoThread)

    stdin = io.StringIO('{"type": "agent_output", "change": {"title": "T"}, "payload": {}}\n')
    rc = imp_telegram.main(
        env={"IMP_TELEGRAM_TOKEN": "tok", "IMP_TELEGRAM_CHAT_ID": "5"},
        stdin=stdin, stdout=io.StringIO())
    assert rc == 0
    assert get_offsets == [0]                  # drained pre-startup backlog once
    assert len(sent) == 1                       # the agent_output event was relayed
    assert sent[0][0] == 5
