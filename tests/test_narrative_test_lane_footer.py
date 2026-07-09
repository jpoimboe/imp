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
    assert b.current_activity().verb == "waiting to test"


def test_test_lane_acquired_restores_footer_verb() -> None:
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    assert b.current_activity().verb == "waiting to test"
    b.add(ev("4", "test_lane_acquired", step="integration-tests"))
    assert b.current_activity().verb == "running tests"


def test_waiting_for_test_lane_sets_activity_lane_wait() -> None:
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    act = b.current_activity()
    assert act.lane_wait is True
    assert act.kind == "script"


def test_test_lane_acquired_clears_activity_lane_wait() -> None:
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    b.add(ev("4", "test_lane_acquired", step="integration-tests"))
    assert b.current_activity().lane_wait is False


def test_waiting_for_test_lane_leaves_no_narrative_node() -> None:
    from imp.cli.render_text import render_tree
    b = _build()
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests"))
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "waiting to test" not in out


def _breadcrumbs(node) -> list[nv.BreadcrumbLine]:
    out: list[nv.BreadcrumbLine] = []
    if isinstance(node, nv.BreadcrumbLine):
        out.append(node)
    for child in getattr(node, "children", []):
        out.extend(_breadcrumbs(child))
    return out


def test_test_lane_acquired_restarts_step_clock_excluding_queue_wait() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="integration-tests", kind="script",
             phase="integration", label="running tests",
             timestamp="2026-01-01T00:00:00+00:00"))
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests",
             timestamp="2026-01-01T00:00:10+00:00"))
    # Slot acquired 20s after queueing — that queue wait must NOT count toward the step.
    b.add(ev("4", "test_lane_acquired", step="integration-tests",
             timestamp="2026-01-01T00:00:30+00:00"))
    b.add(ev("5", "step_completed", step="integration-tests", kind="script",
             action={"verb": "continue"}, var_mutations={},
             timestamp="2026-01-01T00:00:35+00:00"))
    crumbs = _breadcrumbs(b.tree.root)
    assert len(crumbs) == 1
    assert crumbs[0].elapsed_s == 5.0   # pure run time (acquired..completed), queue excluded
