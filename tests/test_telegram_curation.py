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


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("reauth_needed", "needs re-auth"),
        ("retry_ceiling", "stopped after repeated errors"),
        ("context_window", "hit the context limit"),
        ("max_loops", "stuck in a loop"),
        ("unknown_step", "workflow error"),
    ],
)
def test_paused_body_maps_reason_to_plain_speak(reason: str, expected: str) -> None:
    assert imp_telegram.event_body("workflow_paused", {"reason": reason}) == expected


def test_paused_body_unmapped_reason_falls_back() -> None:
    body = imp_telegram.event_body("workflow_paused", {"reason": "context_shift"})
    assert body == "paused (context_shift)"


def test_paused_body_missing_reason_is_bare_paused() -> None:
    assert imp_telegram.event_body("workflow_paused", {}) == "paused"


def test_paused_message_has_header_and_plain_speak_body() -> None:
    event = {
        "type": "workflow_paused",
        "change": {"title": "Foo"},
        "payload": {"reason": "reauth_needed"},
    }
    assert imp_telegram.format_event_message(event) == "[Foo] paused\nneeds re-auth"


# A silenced pause must produce no body, so the adapter's
# `kind == "silent" and not body.strip()` guard drops it (no redundant quiet line).
@pytest.mark.parametrize("reason", ["merge_blocked", "script_blocked", "interrupt"])
def test_silenced_paused_has_empty_body(reason: str) -> None:
    assert imp_telegram.event_body("workflow_paused", {"reason": reason}) == ""


def test_pause_is_silent_predicate_reflects_denylist() -> None:
    # A reason in the denylist is silent; a companion-less reason is not.
    assert imp_telegram._pause_is_silent({"reason": "interrupt"}) is True
    assert imp_telegram._pause_is_silent({"reason": "max_loops"}) is False
    assert imp_telegram._pause_is_silent({}) is False
