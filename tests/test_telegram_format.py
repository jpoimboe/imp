from __future__ import annotations

import os
import time

import pytest

from imp.remote.drivers import telegram as imp_telegram


def test_format_awaiting_user_input_uses_real_steer_hint_payload() -> None:
    # Every provider emits awaiting_user_input with payload {"steer_hint": ...} (NOT
    # "text"/"message"), so the body must come from "steer_hint".
    event = {
        "type": "awaiting_user_input",
        "quest": {"title": "My Quest", "id": "2026-06-11-abc"},
        "payload": {"steer_hint": "Which DB should I use?"},
    }
    # The loud body is the humanized label alone — "needs your input", not the raw
    # event type, and NO "[title]" bracket (identity rides the header_line).
    assert imp_telegram.format_event_message(event) == (
        "needs your input\nWhich DB should I use?")


def test_format_agent_message_renders_assistant_text_without_type_header() -> None:
    # agent_message carries the assistant turn under payload["raw"]; the prose must
    # be rendered (provider-aware) and sent ALONE — no "[title] agent_message"
    # header leaking the internal event type into a narrative message.
    event = {
        "type": "agent_message",
        "quest": {"title": "T"},
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
        "quest": {"title": "T"},
        "payload": {"reason": "conflict", "base": "main",
                    "detail": "CONFLICT (content): merge conflict in x.py"},
    }
    assert imp_telegram.format_event_message(event) == (
        "merge blocked\nconflict\nCONFLICT (content): merge conflict in x.py")


def test_format_loud_body_is_label_only_no_title_bracket() -> None:
    # No '[title]' bracket regardless of the change block — the body is the label.
    event = {"type": "quest_merged", "quest": {"id": "2026-06-11-abc"}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "done"


def test_format_loud_body_is_label_only_even_with_empty_change_block() -> None:
    event = {"type": "quest_merged", "quest": {}, "payload": {}}
    assert imp_telegram.format_event_message(event) == "done"


def test_format_uses_payload_message_key_as_body() -> None:
    event = {
        "type": "quest_merged",
        "quest": {"title": "T"},
        "payload": {"message": "wrapped up"},
    }
    assert imp_telegram.format_event_message(event) == "done\nwrapped up"


def test_split_text_short_returns_single_chunk() -> None:
    from imp.remote.drivers.telegram import split_text
    assert split_text("hello") == ["hello"]


def test_split_text_empty_returns_single_empty_chunk() -> None:
    from imp.remote.drivers.telegram import split_text
    assert split_text("") == [""]


def test_split_text_exactly_max_is_single_chunk() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, split_text
    text = "x" * TELEGRAM_MAX_TEXT
    assert split_text(text) == [text]


def test_split_text_over_limit_splits_and_preserves_everything() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, split_text
    text = "a" * 5000
    chunks = split_text(text)
    assert len(chunks) == 2
    assert all(len(c) <= TELEGRAM_MAX_TEXT for c in chunks)
    assert "".join(chunks) == text


def test_split_text_hard_splits_single_unbroken_long_line() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, split_text
    # A single 5000-char line with no newline must still be hard-split so no
    # chunk exceeds the protocol limit; the first chunk is a full 4096.
    text = "z" * 5000
    chunks = split_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == TELEGRAM_MAX_TEXT
    assert len(chunks[1]) == 5000 - TELEGRAM_MAX_TEXT
    assert "".join(chunks) == text


def test_split_text_packs_on_line_boundaries_without_mid_line_cut() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, split_text
    # 60 lines of 100 chars + '\n' = 6060 chars > 4096, forcing >1 chunk. Each
    # line fits the limit, so every break lands on a line boundary: no chunk cuts
    # mid-line, i.e. each non-final chunk ends with '\n'.
    text = "".join(f"{'x' * 100}\n" for _ in range(60))
    chunks = split_text(text)
    assert len(chunks) >= 2
    assert all(len(c) <= TELEGRAM_MAX_TEXT for c in chunks)
    assert "".join(chunks) == text
    for c in chunks[:-1]:
        assert c.endswith("\n")


def test_split_text_mixed_short_and_overlong_lines() -> None:
    from imp.remote.drivers.telegram import TELEGRAM_MAX_TEXT, split_text
    text = "hello\n" + "z" * 5000 + "\nworld"
    chunks = split_text(text)
    assert all(len(c) <= TELEGRAM_MAX_TEXT for c in chunks)
    assert "".join(chunks) == text
    assert chunks[0] == "hello\n"
    assert chunks[1] == "z" * TELEGRAM_MAX_TEXT
    assert chunks[2] == "z" * (5000 - TELEGRAM_MAX_TEXT) + "\nworld"


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
        "quest": {"id": "tozi28", "title": "Telegram subscriptions"},
        "timestamp": "2026-06-25T14:32:05+00:00",
    }
    assert header_line(event) == "tozi28 · Telegram subscriptions · 2:32pm"


def test_header_line_collapses_to_id_time_when_no_title(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("UTC")
    # No title yet (session started before the rename): collapse to 'id · time',
    # never 'id · id · time'.
    event = {"quest": {"id": "abc123"}, "timestamp": "2026-06-25T11:05:00+00:00"}
    assert header_line(event) == "abc123 · 11:05am"


def test_header_line_collapses_when_title_equals_id(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("UTC")
    event = {"quest": {"id": "abc123", "title": "abc123"},
             "timestamp": "2026-06-25T11:05:00+00:00"}
    assert header_line(event) == "abc123 · 11:05am"


def test_header_line_collapses_when_no_id_or_title(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("UTC")
    event = {"quest": {}, "timestamp": "2026-06-25T11:05:00+00:00"}
    assert header_line(event) == " · 11:05am"


def test_header_line_uses_event_time_in_daemon_local_zone(set_tz) -> None:
    from imp.remote.drivers.telegram import header_line

    set_tz("America/New_York")
    event = {
        "quest": {"id": "tozi28", "title": "T"},
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
        {"type": "script_output", "quest": {"id": "abc", "title": "t"},
         "payload": {"step": "plan-finalized", "stdout": "✓ plan 1/1 · 2 tasks"}})
    assert msg == "✓ plan 1/1 · 2 tasks"


def test_event_labels_humanize_every_buzz_type() -> None:
    assert imp_telegram._EVENT_LABELS == {
        "awaiting_user_input": "needs your input",
        "merge_blocked": "merge blocked",
        "script_blocked": "script blocked",
        "quest_merged": "done",
        "quest_abandoned": "abandoned",
        "workflow_paused": "paused",
        "workflow_resumed": "resumed",
        "error": "error",
    }
    assert "completed" not in imp_telegram._EVENT_LABELS
