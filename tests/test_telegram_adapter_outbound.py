from __future__ import annotations

import inspect
from typing import Any

import pytest

from imp.remote.daemon import telegram_adapter
from imp.remote.daemon.telegram_adapter import format_outbound, send_message
from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT

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


def _capture(monkeypatch: Any) -> list[str]:
    """Monkeypatch api_call and return the list that accumulates each sent text."""
    sent: list[str] = []

    def fake_api_call(token: str, method: str, params: dict) -> dict:
        sent.append(params["text"])
        return {"ok": True, "result": {"message_id": len(sent)}}

    monkeypatch.setattr(telegram_adapter, "api_call", fake_api_call)
    return sent


def test_send_message_strips_armed_escape_codes(monkeypatch: Any) -> None:
    sent = _capture(monkeypatch)
    send_message(
        "tok", 4242, "\x1b[2m—\x1b[0m \x1b[1;33mimplement\x1b[0m", silent=True)
    assert sent == ["— implement"]


def test_send_message_strips_bare_sgr_as_agents_type_it(monkeypatch: Any) -> None:
    # No ESC byte: the spelling a model actually emits, which the CLI re-arms and
    # Telegram cannot. It must strip identically to the armed form above.
    sent = _capture(monkeypatch)
    send_message("tok", 4242, "[2m—[0m [1;33mimplement[0m", silent=True)
    assert sent == ["— implement"]


@pytest.mark.parametrize("seq", [
    "\x1b[2J",             # CSI beyond color: erase display
    "\x1b[?25l",           # private-mode CSI: hide cursor
    "\x1b]0;title\x07",    # OSC, BEL-terminated
    "\x1b]0;title\x1b\\",  # OSC, ST-terminated — the trailing backslash goes too
    "\x1bPq###\x1b\\",     # DCS string escape — payload goes with it, not visible
    "\x1b_apc\x1b\\",      # APC string escape
    "\x1b^pm\x1b\\",       # PM string escape
    "\x1bXsos\x1b\\",      # SOS string escape
    "\x1bM",               # two-character escape — the M goes with the ESC
    "\x1b(B",              # escape with an intermediate byte (terminfo sgr0 emits it)
    "\r",                  # stray C0 byte
])
def test_send_message_removes_every_escape_family(
    monkeypatch: Any, seq: str,
) -> None:
    sent = _capture(monkeypatch)
    send_message("tok", 4242, f"a{seq}b", silent=True)
    assert sent == ["ab"]


@pytest.mark.parametrize("opener", ["\x1b]0;", "\x1bP", "\x1b_", "\x1b^", "\x1bX"])
def test_send_message_swallows_an_unterminated_string_escape(
    monkeypatch: Any, opener: str,
) -> None:
    # No BEL or ST arrives: the payload must go with the opener rather than land in
    # the chat as visible text.
    sent = _capture(monkeypatch)
    send_message("tok", 4242, f"a{opener}title", silent=True)
    assert sent == ["a"]


def test_send_message_bounds_an_unterminated_string_escape_to_its_line(
    monkeypatch: Any,
) -> None:
    # The swallow stops at the newline, so prose on later lines survives.
    sent = _capture(monkeypatch)
    send_message("tok", 4242, "a\x1b]0;title\nnext line", silent=True)
    assert sent == ["a\nnext line"]


def test_send_message_keeps_newline_and_tab(monkeypatch: Any) -> None:
    sent = _capture(monkeypatch)
    send_message("tok", 4242, "a\nb\tc", silent=True)
    assert sent == ["a\nb\tc"]


@pytest.mark.parametrize("text", [
    "model claude-opus-4-8[1m] chosen",   # the (?!\]) guard spares imp's model ids
    r"write \x1b[31m to arm red",         # backslash spelling: source text, not a screen
])
def test_send_message_preserves_ineligible_bare_spellings(
    monkeypatch: Any, text: str,
) -> None:
    sent = _capture(monkeypatch)
    send_message("tok", 4242, text, silent=True)
    assert sent == [text]


def test_send_message_strips_before_chunking(monkeypatch: Any) -> None:
    sent = _capture(monkeypatch)
    body = ("\x1b[31m" + "a" * 4 + "\x1b[0m") * 500
    assert len(body) > TELEGRAM_MAX_TEXT   # over the limit ONLY because of escapes
    send_message("tok", 4242, body, silent=True)
    assert sent == ["a" * 2000]


def test_send_message_passes_escape_free_text_through_byte_identical(
    monkeypatch: Any,
) -> None:
    sent = _capture(monkeypatch)
    reply = ("Alerts: subscribed only\n"
             "● @k3f9qa  Dark mode — running · plan\n"
             "@m7x2  Telegram polish — paused · review")
    send_message("tok", 4242, reply, silent=True)
    assert sent == [reply]
