"""Live footer wait-state: a hinted agent_retry replaces the count-up work verb
with a wait verb + countdown_total, preserving the step's position and model."""
from __future__ import annotations

import pytest

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="deferred-sweep", kind="agent",
             phase="convergence", phase_label="convergence phase",
             label="sweeping deferred tasks", model="claude"))
    return b


def test_footer_verb_before_wait_is_step_phrase() -> None:
    b = _build()
    act = b.current_activity()
    assert act.verb == "sweeping deferred tasks"
    assert act.countdown_total is None


def test_hinted_rate_limit_retry_sets_wait_and_countdown() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    act = b.current_activity()
    assert act.verb == "waiting for limit reset"
    assert act.countdown_total == 2940.0
    # position breadcrumb and model are preserved (not None with a step on the stack)
    assert act.position == "convergence phase · sweeping deferred tasks"
    assert act.model == "claude"


def test_hinted_auth_retry_uses_retry_wording() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="auth", attempt=1,
             backoff_seconds=300.0, hinted=True))
    assert b.current_activity().verb == "waiting to retry"


def test_long_backoff_without_hint_flag_sets_wait() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=2,
             backoff_seconds=7200.0))
    act = b.current_activity()
    assert act.verb == "waiting for limit reset"
    assert act.countdown_total == 7200.0


def test_short_blind_retry_leaves_wait_unset() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=3,
             backoff_seconds=60.0))
    act = b.current_activity()
    assert act.verb == "sweeping deferred tasks"
    assert act.countdown_total is None
    assert b._reset_wait is None


def test_stalled_retry_leaves_wait_unset() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="stalled", attempt=2,
             backoff_seconds=4.0))
    assert b._reset_wait is None


def test_second_hinted_retry_overwrites_countdown() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    b.add(ev("4", "agent_retry", category="rate_limit", attempt=2,
             backoff_seconds=1200.0, hinted=True))
    assert b.current_activity().countdown_total == 1200.0


def test_wait_with_empty_stack_yields_none_position_and_model() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    act = b.current_activity()
    assert act.verb == "waiting for limit reset"
    assert act.countdown_total == 2940.0
    assert act.position is None
    assert act.model is None


def _build_with_wait() -> nv.NarrativeBuilder:
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    assert b._reset_wait is not None
    return b


@pytest.mark.parametrize("etype, payload", [
    ("user_message", {"text": "hi"}),
    ("llm_response", {"provider": "anthropic", "model": "m",
                      "stop_reason": "end_turn", "usage": {}, "raw": {"content": []}}),
    ("thinking", {"content": "x"}),
    ("tool_call", {"id": "z", "name": "read_file", "input": {}}),
    ("agent_output", {"text": "ok"}),
    ("step_started", {"step": "s2", "kind": "agent", "phase": "convergence"}),
    ("step_completed", {"step": "deferred-sweep", "kind": "agent",
                        "action": {"verb": "continue"}, "var_mutations": {}}),
])
def test_resume_event_clears_wait_state(etype: str, payload: dict) -> None:
    b = _build_with_wait()
    b.add({"id": "9", "agent_id": None, "type": etype, "payload": payload})
    assert b._reset_wait is None


def test_resume_event_reverts_footer_verb() -> None:
    b = _build_with_wait()
    b.add(ev("9", "thinking", agent_id="step:convergence/deferred-sweep", content="x"))
    assert b.current_activity().verb == "sweeping deferred tasks"


def test_non_resume_event_leaves_wait_set() -> None:
    b = _build_with_wait()
    b.add(ev("9", "warning", message="a transient note"))
    assert b._reset_wait is not None
    assert b.current_activity().verb == "waiting for limit reset"
