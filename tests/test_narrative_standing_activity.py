# tests/test_narrative_standing_activity.py
"""The step-free footer activity: the init setup host emits no step_started, so
its footer stands on an explicitly installed activity instead of the step stack."""
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev

T0 = "2026-01-01T00:00:00+00:00"


def _standing() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.set_standing_activity(verb="thinking", model="claude-sonnet-4-5", start_ts=T0)
    return b


def test_a_step_free_builder_is_idle_without_a_standing_activity() -> None:
    # The frozen-footer bug: no step stack, no activity, LiveStatus draws nothing.
    assert nv.NarrativeBuilder().current_activity() is None


def test_standing_activity_is_agent_kind_and_carries_verb_and_model() -> None:
    act = _standing().current_activity()
    assert act is not None
    assert act.verb == "thinking"
    assert act.model == "claude-sonnet-4-5"
    # agent-kind is what opens compose_position_row's context-bar branch.
    assert act.kind == "agent"
    assert act.lane_wait is False
    # Setup has no quest position: no phase, plan or step counter segments.
    assert (act.position, act.phase, act.plan, act.counter) == (None, None, None, None)


def test_standing_activity_survives_the_events_of_a_turn() -> None:
    b = _standing()
    b.add(ev("1", "tool_call", agent_id="a", id="c1", name="read_file",
             input={"path": "app.py"}))
    b.add(ev("2", "tool_result", agent_id="a", id="c1", content="ok"))
    b.add(ev("3", "agent_message", agent_id="a", provider="anthropic",
             raw={"content": [{"type": "text", "text": "which theme?"}]}, usage={}))
    act = b.current_activity()
    assert act is not None and act.verb == "thinking"   # never goes idle mid-turn


def test_open_burst_tail_rides_alongside_the_standing_verb() -> None:
    b = _standing()
    b.add(ev("1", "tool_call", agent_id="a", id="c1", name="read_file",
             input={"path": "app.py"}))
    act = b.current_activity()
    assert act is not None
    assert act.verb == "thinking"                        # the tail never replaces the verb
    assert act.detail == "1 call · read_file app.py"


def test_standing_clock_starts_at_the_installed_stamp() -> None:
    act = _standing().current_activity()
    assert act is not None
    assert nv.activity_elapsed(act, "2026-01-01T00:00:09+00:00") == 9.0


def test_user_message_restarts_the_standing_clock() -> None:
    b = _standing()
    b.add(ev("1", "user_message", agent_id="a", subtype="reply",
             content=[{"type": "text", "text": "dark"}],
             timestamp="2026-01-01T00:00:30+00:00"))
    act = b.current_activity()
    assert act is not None
    # Elapsed is the age of THIS turn, not of the whole conversation.
    assert nv.activity_elapsed(act, "2026-01-01T00:00:34+00:00") == 4.0


def test_a_step_supersedes_the_standing_activity() -> None:
    # Guard for the run path: a real step keeps owning the footer.
    b = _standing()
    b.add(ev("1", "step_started", step="spec", kind="agent", phase="design",
             model="m", provider="anthropic", label="drafting"))
    act = b.current_activity()
    assert act is not None
    assert act.verb == "drafting" and act.model == "m"
