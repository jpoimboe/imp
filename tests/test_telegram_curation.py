from __future__ import annotations

import pytest

from imp.remote.drivers import telegram as imp_telegram


@pytest.mark.parametrize(
    "event_type",
    [
        "awaiting_user_input", "merge_blocked", "script_blocked", "change_merged",
        "change_abandoned", "workflow_paused", "workflow_resumed",
    ],
)
def test_buzz_event_types_notify(event_type: str) -> None:
    assert imp_telegram.curate(event_type, {}) == "buzz"


@pytest.mark.parametrize(
    "event_type", ["llm_response", "agent_output", "notice", "step_started"])
def test_silent_event_types_stream_quietly(event_type: str) -> None:
    assert imp_telegram.curate(event_type, {}) == "silent"


@pytest.mark.parametrize(
    "event_type",
    [
        "tool_call", "tool_result", "thinking", "step_context",
        "llm_response_usage_patch", "skill_load", "step_label_updated",
        "user_message", "user_interjected", "change_created", "change_renamed",
        "cli_attached", "runner_warning", "tool_killed", "completed",
    ],
)
def test_dropped_event_types_are_dropped(event_type: str) -> None:
    assert imp_telegram.curate(event_type, {}) is None


def test_unknown_event_type_is_dropped() -> None:
    assert imp_telegram.curate("something_new", {}) is None
    assert imp_telegram.curate(None, {}) is None


def test_terminal_error_buzzes() -> None:
    assert imp_telegram.curate("error", {"category": "runtime", "kind": "boom"}) == "buzz"


def test_nonterminal_interrupt_error_is_dropped() -> None:
    assert imp_telegram.curate(
        "error", {"category": "config", "kind": "interrupted"}) is None


def test_errored_is_not_a_buzz_event_or_label() -> None:
    assert "errored" not in imp_telegram._BUZZ
    assert "errored" not in imp_telegram._EVENT_LABELS
