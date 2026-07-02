"""The live footer verb must reflect a test-lane block. The step's `started`
fires before the lane is acquired, so current_activity() would otherwise read the
step's "running tests" phrase for a change that is actually parked waiting for a
slot (all parallel changes looked like they were running tests). waiting_for_test_lane
overrides the footer verb; test_lane_acquired restores it."""
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="integration-tests", kind="script",
           phase="integration", label="running tests"),
    ]:
        b.add(e)
    return b


def test_footer_verb_before_waiting_is_running_tests() -> None:
    b = _build()
    assert b.current_activity().verb == "running tests"


def test_waiting_for_test_lane_overrides_footer_verb() -> None:
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    assert b.current_activity().verb == "waiting for a test slot"


def test_test_lane_acquired_restores_footer_verb() -> None:
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    assert b.current_activity().verb == "waiting for a test slot"
    b.add(ev("4", "test_lane_acquired", step="integration-tests"))
    assert b.current_activity().verb == "running tests"


def test_waiting_for_test_lane_still_renders_narrative_node() -> None:
    from imp.cli.render_text import render_tree
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "waiting for a test slot" in out
