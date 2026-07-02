from __future__ import annotations

import pytest

from imp.remote.drivers import telegram as imp_telegram


@pytest.mark.parametrize(
    "event_type",
    ["awaiting_user_input", "merge_blocked", "script_blocked", "change_merged"],
)
def test_buzz_event_types_notify(event_type: str) -> None:
    assert imp_telegram.curate(event_type, {}) == "buzz"


@pytest.mark.parametrize(
    "event_type",
    [
        "llm_response", "agent_output", "script_output", "step_started",
        "workflow_resumed", "change_abandoned",
    ],
)
def test_silent_event_types_stream_quietly(event_type: str) -> None:
    assert imp_telegram.curate(event_type, {}) == "silent"


# one buzz per stop. A workflow_paused whose reason means a companion
# event already buzzed the stop (or the stop was self-inflicted) is silent.
@pytest.mark.parametrize(
    "reason",
    [
        "merge_blocked", "script_blocked", "stalled", "agent_output_missing",
        "awaiting_turn", "interrupt",
    ],
)
def test_workflow_paused_silent_when_companion_buzzed(reason: str) -> None:
    assert imp_telegram.curate("workflow_paused", {"reason": reason}) == "silent"


# A companion-less pause reason has no other buzz, so the paused event must buzz.
@pytest.mark.parametrize(
    "reason",
    ["reauth_needed", "retry_ceiling", "context_window", "max_loops", "unknown_step"],
)
def test_workflow_paused_buzzes_when_companion_less(reason: str) -> None:
    assert imp_telegram.curate("workflow_paused", {"reason": reason}) == "buzz"


def test_workflow_paused_unknown_reason_fails_safe_to_buzz() -> None:
    assert imp_telegram.curate("workflow_paused", {"reason": "some_future_reason"}) == "buzz"
    assert imp_telegram.curate("workflow_paused", {}) == "buzz"


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
