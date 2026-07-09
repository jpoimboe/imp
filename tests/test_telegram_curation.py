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
        "merge_blocked", "script_blocked", "agent_output_missing", "awaiting_turn",
        "interrupt", "api", "quota", "content_filter", "loop", "terminated",
        "unknown", "config", "internal_error", "retry_ceiling", "reauth_needed",
    ],
)
def test_workflow_paused_silent_when_companion_buzzed(reason: str) -> None:
    assert imp_telegram.curate("workflow_paused", {"reason": reason}) == "silent"


# A companion-less pause reason has no other buzz, so the paused event must buzz.
@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "max_loops"},
        {"reason": "unknown_step"},
        {"reason": "clean_tree"},
        {"reason": "context_window", "kind": "reincarnation_cap"},
    ],
)
def test_workflow_paused_buzzes_when_companion_less(payload: dict) -> None:
    assert imp_telegram.curate("workflow_paused", payload) == "buzz"


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
    "payload,expected",
    [
        ({"reason": "context_window", "kind": "reincarnation_cap"}, "hit the context limit"),
        ({"reason": "max_loops"}, "stuck in a loop"),
        ({"reason": "unknown_step"}, "workflow error"),
    ],
)
def test_paused_body_maps_reason_to_plain_speak(payload: dict, expected: str) -> None:
    assert imp_telegram.event_body("workflow_paused", payload) == expected


def test_paused_body_unmapped_reason_falls_back() -> None:
    body = imp_telegram.event_body("workflow_paused", {"reason": "context_shift"})
    assert body == "paused (context_shift)"


def test_paused_body_missing_reason_is_bare_paused() -> None:
    assert imp_telegram.event_body("workflow_paused", {}) == "paused"


def test_paused_message_has_header_and_plain_speak_body() -> None:
    event = {
        "type": "workflow_paused",
        "change": {"title": "Foo"},
        "payload": {"reason": "max_loops"},
    }
    assert imp_telegram.format_event_message(event) == "[Foo] paused\nstuck in a loop"


# A silenced pause must produce no body, so the adapter's
# `kind == "silent" and not body.strip()` guard drops it (no redundant quiet line).
@pytest.mark.parametrize(
    "reason", ["merge_blocked", "script_blocked", "interrupt", "retry_ceiling", "reauth_needed"],
)
def test_silenced_paused_has_empty_body(reason: str) -> None:
    assert imp_telegram.event_body("workflow_paused", {"reason": reason}) == ""


def test_pause_is_silent_predicate_reflects_denylist() -> None:
    # A reason in the denylist is silent; a companion-less reason is not.
    assert imp_telegram._pause_is_silent({"reason": "interrupt"}) is True
    assert imp_telegram._pause_is_silent({"reason": "max_loops"}) is False
    assert imp_telegram._pause_is_silent({}) is False


def test_pause_silent_reasons_exact_set() -> None:
    expected = frozenset({
        "merge_blocked", "script_blocked", "agent_output_missing", "awaiting_turn",
        "interrupt", "awaiting_user", "api", "quota", "content_filter", "loop",
        "terminated", "unknown", "config", "internal_error", "retry_ceiling",
        "reauth_needed",
    })
    assert expected == imp_telegram._PAUSE_SILENT_REASONS


def test_awaiting_user_pause_is_silent() -> None:
    # A your-turn park is quiet in the stream — parity with the old "interrupt"
    # tier — rather than newly buzzing. The "needs your input" buzz stays the
    # real signal elsewhere.
    assert imp_telegram.curate(
        "workflow_paused", {"reason": "awaiting_user"}) == "silent"
    assert imp_telegram._pause_is_silent({"reason": "awaiting_user"}) is True
    assert imp_telegram.event_body(
        "workflow_paused", {"reason": "awaiting_user"}) == ""


def test_pause_reason_bodies_exact() -> None:
    assert imp_telegram._PAUSE_REASON_BODIES == {
        "context_window": "hit the context limit",
        "max_loops": "stuck in a loop",
        "unknown_step": "workflow error",
    }


def test_reincarnation_cap_stop_buzzes_with_body() -> None:
    payload = {"reason": "context_window", "kind": "reincarnation_cap"}
    assert imp_telegram.curate("workflow_paused", payload) == "buzz"
    assert imp_telegram.event_body("workflow_paused", payload) == "hit the context limit"


def test_tier_b_context_window_is_silent_with_empty_body() -> None:
    payload = {"reason": "context_window", "kind": "context_length_exceeded"}
    assert imp_telegram.curate("workflow_paused", payload) == "silent"
    assert imp_telegram.event_body("workflow_paused", payload) == ""


def test_context_window_without_kind_is_silent() -> None:
    # A context_window pause carries a kind in practice; a kindless one defaults to
    # silent (kind != "reincarnation_cap"), never silencing a genuine companion-less stop.
    assert imp_telegram.curate("workflow_paused", {"reason": "context_window"}) == "silent"
    assert imp_telegram.event_body("workflow_paused", {"reason": "context_window"}) == ""


def test_self_inflicted_config_interrupt_yields_zero_buzzes() -> None:
    companion = imp_telegram.curate("error", {"category": "config", "kind": "interrupted"})
    pause = imp_telegram.curate("workflow_paused", {"reason": "interrupt"})
    assert companion is None
    assert pause == "silent"
    assert [companion, pause].count("buzz") == 0


@pytest.mark.parametrize(
    "err_category,err_kind,pause_reason,pause_kind",
    [
        ("api", "server_error", "api", None),
        ("quota", "insufficient_quota", "quota", None),
        ("content_filter", "content_policy", "content_filter", None),
        ("loop", "tool_loop", "loop", None),
        ("terminated", "sigterm", "terminated", None),
        ("unknown", "mystery", "unknown", None),
        ("config", "invalid", "config", None),
        ("context_window", "context_length_exceeded", "context_window", "context_length_exceeded"),
        ("internal", "exception", "internal_error", None),
        ("network", "timeout", "retry_ceiling", None),
        ("rate_limit", "429", "retry_ceiling", None),
        ("stalled", "watchdog", "retry_ceiling", None),
        ("auth", "token_expired", "reauth_needed", None),
    ],
)
def test_one_buzz_per_error_driven_stop(
    err_category: str, err_kind: str, pause_reason: str, pause_kind: str | None,
) -> None:
    companion = imp_telegram.curate("error", {"category": err_category, "kind": err_kind})
    pause = imp_telegram.curate(
        "workflow_paused", {"reason": pause_reason, "kind": pause_kind})
    assert [companion, pause].count("buzz") == 1
