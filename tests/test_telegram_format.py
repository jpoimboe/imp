from __future__ import annotations

import os
import time

import pytest

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
    event = {"type": "change_merged", "change": {"id": "2026-06-11-abc"}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "[2026-06-11-abc] done"


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
    event = {"type": "change_merged", "change": {}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "[imp] done"


def test_format_uses_payload_message_key_as_body() -> None:
    event = {
        "type": "change_merged",
        "change": {"title": "T"},
        "payload": {"message": "wrapped up"},
    }
    assert imp_telegram.format_event_message(event) == "[T] done\nwrapped up"


def test_clamp_text_passes_exactly_max_length_unchanged() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, clamp_text
    text = "x" * TELEGRAM_MAX_TEXT
    assert clamp_text(text) == text


@pytest.fixture
def set_tz():
    saved = os.environ.get("TZ")

    def _apply(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _apply
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def test_format_time_of_day_pins_ampm_lowercase_no_leading_zero(set_tz) -> None:
    from imp.remote.drivers.telegram import format_time_of_day

    set_tz("UTC")
    assert format_time_of_day("2026-06-25T14:32:05+00:00") == "2:32pm"
    assert format_time_of_day("2026-06-25T11:05:00+00:00") == "11:05am"
    assert format_time_of_day("2026-06-25T00:15:00+00:00") == "12:15am"
    assert format_time_of_day("2026-06-25T12:00:00+00:00") == "12:00pm"


def test_format_time_of_day_converts_utc_to_daemon_local_zone(set_tz) -> None:
    # EDT = UTC-4: 14:32 UTC → 10:32am, proving daemon-local conversion (not raw UTC).
    from imp.remote.drivers.telegram import format_time_of_day

    set_tz("America/New_York")
    assert format_time_of_day("2026-06-25T14:32:05+00:00") == "10:32am"


def test_step_divider_uses_phase_label_then_label() -> None:
    from imp.remote.drivers.telegram import step_divider

    event = {
        "type": "step_started",
        "payload": {"phase_label": "Brainstorm", "label": "scoping the change"},
    }
    assert step_divider(event) == "— Brainstorm › scoping the change —"


def test_step_divider_label_only_when_no_phase_label() -> None:
    from imp.remote.drivers.telegram import step_divider

    assert step_divider(
        {"type": "step_started", "payload": {"label": "planning"}}) == "— planning —"


def test_step_divider_none_when_label_empty() -> None:
    from imp.remote.drivers.telegram import step_divider

    assert step_divider(
        {"type": "step_started",
         "payload": {"phase_label": "Brainstorm", "label": ""}}) is None


def test_step_divider_none_when_label_whitespace_only() -> None:
    from imp.remote.drivers.telegram import step_divider

    assert step_divider(
        {"type": "step_started", "payload": {"label": "   "}}) is None


def test_step_divider_none_when_show_label_false() -> None:
    from imp.remote.drivers.telegram import step_divider

    assert step_divider(
        {"type": "step_started",
         "payload": {"label": "internal setup", "show_label": False}}) is None


def test_header_line_renders_id_name_and_event_local_time(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("UTC")
    event = {
        "change": {"id": "tozi28", "title": "Telegram subscriptions"},
        "timestamp": "2026-06-25T14:32:05+00:00",
    }
    assert header_line(event) == "tozi28 · Telegram subscriptions · 2:32pm"


def test_header_line_name_falls_back_to_the_id(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("UTC")
    event = {"change": {"id": "abc123"}, "timestamp": "2026-06-25T11:05:00+00:00"}
    assert header_line(event) == "abc123 · abc123 · 11:05am"


def test_header_line_name_falls_back_to_imp_when_no_id_or_title(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("UTC")
    event = {"change": {}, "timestamp": "2026-06-25T11:05:00+00:00"}
    assert header_line(event) == " · imp · 11:05am"


def test_header_line_uses_event_time_in_daemon_local_zone(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("America/New_York")
    event = {
        "change": {"id": "tozi28", "title": "T"},
        "timestamp": "2026-06-25T14:32:05+00:00",
    }
    assert header_line(event) == "tozi28 · T · 10:32am"


def test_script_output_body_surfaces_stdout() -> None:
    from imp.remote.drivers.telegram import event_body, format_event_message

    assert event_body(
        "script_output",
        {"step": "plan-finalized", "stdout": "✓ plan 1/1 · 2 tasks"},
    ) == "✓ plan 1/1 · 2 tasks"
    msg = format_event_message(
        {"type": "script_output", "change": {"id": "abc", "title": "t"},
         "payload": {"step": "plan-finalized", "stdout": "✓ plan 1/1 · 2 tasks"}})
    assert msg == "✓ plan 1/1 · 2 tasks"


def test_event_labels_humanize_every_buzz_type() -> None:
    assert imp_telegram._EVENT_LABELS == {
        "awaiting_user_input": "needs your input",
        "merge_blocked": "merge blocked",
        "script_blocked": "script blocked",
        "change_merged": "done",
        "change_abandoned": "abandoned",
        "workflow_paused": "paused",
        "workflow_resumed": "resumed",
        "error": "error",
    }
    assert "completed" not in imp_telegram._EVENT_LABELS
