from __future__ import annotations

import io

from imp.remote.drivers import telegram as imp_telegram


def test_format_includes_change_title_event_type_and_body() -> None:
    event = {
        "type": "awaiting_user_input",
        "change": {"title": "My Change", "id": "2026-06-11-abc"},
        "payload": {"text": "need your input"},
    }
    assert imp_telegram.format_event_message(event) == (
        "[My Change] awaiting_user_input\nneed your input")


def test_format_falls_back_to_change_id_then_header_only() -> None:
    event = {"type": "errored", "change": {"id": "2026-06-11-abc"}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "[2026-06-11-abc] errored"


def test_emit_reply_collapses_to_a_single_flushed_line() -> None:
    buf = io.StringIO()
    imp_telegram.emit_reply("line one\nline two\r\nthree", buf)
    # imp reads exactly one line per reply, so internal newlines collapse to
    # spaces and the value is terminated with a single trailing newline.
    assert buf.getvalue() == "line one line two  three\n"


def test_format_falls_back_to_imp_when_no_title_and_no_id() -> None:
    event = {"type": "errored", "change": {}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "[imp] errored"


def test_format_uses_payload_message_key_as_body() -> None:
    event = {
        "type": "errored",
        "change": {"title": "T"},
        "payload": {"message": "something went wrong"},
    }
    assert imp_telegram.format_event_message(event) == "[T] errored\nsomething went wrong"


def test_emit_reply_plain_string() -> None:
    buf = io.StringIO()
    imp_telegram.emit_reply("ok", buf)
    assert buf.getvalue() == "ok\n"
