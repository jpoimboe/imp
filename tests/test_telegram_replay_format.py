from __future__ import annotations

import os
import time

import pytest

from imp.remote.daemon.telegram_adapter import format_replay_event


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


def _event(etype: str, payload: dict) -> dict:
    return {
        "id": "e1", "type": etype, "timestamp": "2026-06-25T14:32:05+00:00",
        "change": {"id": "tozi28", "title": "T"}, "payload": payload,
    }


def test_silent_agent_output_renders_header_and_body_no_footer(set_tz) -> None:
    set_tz("UTC")
    # A silent narrative event carries no reply footer — only the awaiting_user_input
    # gate appends ↩ @handle.
    assert format_replay_event(_event("agent_output", {"text": "hello"})) == (
        "tozi28 · T · 2:32pm\nhello")


def test_buzz_event_renders_header_label_and_footer(set_tz) -> None:
    set_tz("UTC")
    # The gate keeps its ↩ footer; the body is the label alone (no '[T]' bracket).
    assert format_replay_event(
        _event("awaiting_user_input", {"prompt": "go?"})) == (
        "tozi28 · T · 2:32pm\nneeds your input\ngo?\n↩ @tozi28")


def test_step_started_renders_the_header_less_divider() -> None:
    assert format_replay_event(
        _event("step_started", {"phase_label": "Brainstorm", "label": "scoping"})
    ) == "— Brainstorm › scoping —"


def test_dropped_type_returns_none() -> None:
    assert format_replay_event(_event("tool_call", {"name": "bash"})) is None


def test_empty_body_silent_returns_none() -> None:
    assert format_replay_event(_event("agent_output", {"text": ""})) is None


def test_step_started_with_show_label_false_renders_no_divider() -> None:
    assert format_replay_event(
        _event("step_started", {"show_label": False, "label": "x"})) is None


def test_step_started_with_empty_label_returns_none() -> None:
    assert format_replay_event(_event("step_started", {"label": ""})) is None
