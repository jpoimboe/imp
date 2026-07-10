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
    # position breadcrumb is the phase label alone for a non-position_label step
    # (matches steady state, so entering the wait is position-neutral); model preserved
    assert act.position == "convergence phase"
    assert act.model == "claude"


def test_hinted_auth_retry_uses_reauthentication_wording() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="auth", attempt=1,
             backoff_seconds=300.0, hinted=True))
    assert b.current_activity().verb == "waiting for re-authentication"


def test_long_backoff_without_hint_flag_sets_wait() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=2,
             backoff_seconds=7200.0))
    act = b.current_activity()
    assert act.verb == "waiting for limit reset"
    assert act.countdown_total == 7200.0


def test_short_blind_retry_now_sets_wait() -> None:
    # A short, unhinted exponential backoff must now surface the wait + countdown
    # (this is the motivating auth/network series that previously showed nothing).
    b = _build()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=3,
             backoff_seconds=8.0))
    act = b.current_activity()
    assert act.verb == "waiting for limit reset"
    assert act.countdown_total == 8.0
    assert b._reset_wait is not None


def test_stalled_retry_now_sets_wait() -> None:
    # stalled gains the activity-line verb + countdown; its calm scrollback
    # breadcrumb is preserved separately (test_narrative_agent_retry.py).
    b = _build()
    b.add(ev("3", "agent_retry", category="stalled", attempt=2,
             backoff_seconds=4.0))
    act = b.current_activity()
    assert act.verb == "stalled - waiting to retry"
    assert act.countdown_total == 4.0
    assert b._reset_wait is not None


def test_network_retry_uses_network_wording() -> None:
    b = _build()
    b.add(ev("3", "agent_retry", category="network", attempt=1,
             backoff_seconds=16.0))
    assert b.current_activity().verb == "network - waiting to retry"


def test_retry_verb_maps_each_category_and_falls_back() -> None:
    assert nv._retry_verb("rate_limit") == "waiting for limit reset"
    assert nv._retry_verb("auth") == "waiting for re-authentication"
    assert nv._retry_verb("network") == "network - waiting to retry"
    assert nv._retry_verb("stalled") == "stalled - waiting to retry"
    # any category not named above (a future backoff category) → generic verb
    assert nv._retry_verb("something_else") == "waiting to retry"


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


def test_reset_wait_position_matches_steady_state_for_non_position_label() -> None:
    b = _build()
    before = b.current_activity().position
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    during = b.current_activity().position
    assert before == "convergence phase"
    assert during == before


def _build_position_label() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent",
             phase="planning", phase_label="planning phase",
             label="plan 1/3", model="claude", position_label=True))
    return b


def test_reset_wait_position_matches_steady_state_for_position_label() -> None:
    b = _build_position_label()
    before = b.current_activity().position
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    during = b.current_activity().position
    # a position_label step carries its label as a position counter, so the
    # breadcrumb joins phase + label in both steady state and the wait
    assert before == "planning phase · plan 1/3"
    assert during == before


def test_running_subagent_wait_propagates_to_parent() -> None:
    b = _build()
    # Dispatch a subagent (no closing tool_result -> the fold stays running).
    b.add({"id": "tk1", "agent_id": "step:convergence/deferred-sweep",
           "type": "tool_call",
           "payload": {"id": "tk1", "name": "sub_agent",
                       "input": {"subagent_name": "coder",
                                 "task_description": "edit"}}})
    # The subagent hits the limit: its agent_retry routes into the nested builder.
    b.add(ev("r1", "agent_retry", agent_id="sub:tk1",
             category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True))
    act = b.current_activity()
    assert act.verb == "waiting for limit reset"
    assert act.countdown_total == 2940.0
    assert act.detail is None
    # position is the phase label alone: the step label rides as the fold verb here.
    assert act.position == "convergence phase"
    assert act.model == "claude"
