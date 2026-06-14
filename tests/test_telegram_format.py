from __future__ import annotations

from imp.remote.drivers import telegram as imp_telegram


def test_format_awaiting_user_input_uses_real_prompt_payload() -> None:
    # Every provider emits awaiting_user_input with payload {"prompt": ...} (NOT
    # "text"/"message"), so the body must come from "prompt".
    event = {
        "type": "awaiting_user_input",
        "change": {"title": "My Change", "id": "2026-06-11-abc"},
        "payload": {"prompt": "Which DB should I use?"},
    }
    # The loud header is humanized — "needs your input", not the raw event type.
    assert imp_telegram.format_event_message(event) == (
        "[My Change] needs your input\nWhich DB should I use?")


def test_format_llm_response_renders_assistant_text_without_type_header() -> None:
    # llm_response carries the assistant turn under payload["raw"]; the prose must
    # be rendered (provider-aware) and sent ALONE — no "[title] llm_response"
    # header leaking the internal event type into a narrative message.
    event = {
        "type": "llm_response",
        "change": {"title": "T"},
        "payload": {
            "provider": "anthropic", "model": "m",
            "raw": {"content": [{"type": "text", "text": "Here is my answer."}]},
            "usage": {}, "stop_reason": "end_turn",
        },
    }
    assert imp_telegram.format_event_message(event) == "Here is my answer."


def test_format_merge_blocked_surfaces_reason_and_detail() -> None:
    event = {
        "type": "merge_blocked",
        "change": {"title": "T"},
        "payload": {"reason": "conflict", "base": "main",
                    "detail": "CONFLICT (content): merge conflict in x.py"},
    }
    assert imp_telegram.format_event_message(event) == (
        "[T] merge blocked\nconflict\nCONFLICT (content): merge conflict in x.py")


def test_format_falls_back_to_change_id_then_header_only() -> None:
    event = {"type": "errored", "change": {"id": "2026-06-11-abc"}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "[2026-06-11-abc] error"


def test_clamp_text_leaves_short_text_unchanged() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, clamp_text
    assert TELEGRAM_MAX_TEXT == 4096
    assert clamp_text("hello") == "hello"


def test_clamp_text_truncates_overflow_and_marks_it() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, clamp_text
    out = clamp_text("a" * 5000)
    assert len(out) == TELEGRAM_MAX_TEXT          # never exceeds the protocol limit
    assert out.startswith("aaa")                  # the head of the original survives
    assert out.endswith("[truncated]")            # the cut is visible to the reader


def test_format_falls_back_to_imp_when_no_title_and_no_id() -> None:
    event = {"type": "errored", "change": {}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "[imp] error"


def test_format_uses_payload_message_key_as_body() -> None:
    event = {
        "type": "errored",
        "change": {"title": "T"},
        "payload": {"message": "something went wrong"},
    }
    assert imp_telegram.format_event_message(event) == "[T] error\nsomething went wrong"


def test_clamp_text_passes_exactly_max_length_unchanged() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, clamp_text
    text = "x" * TELEGRAM_MAX_TEXT
    assert clamp_text(text) == text
