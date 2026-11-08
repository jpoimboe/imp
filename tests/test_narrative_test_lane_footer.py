"""The live footer verb must reflect a test-lane block. The step's `started`
fires before the lane is acquired, so current_activity() would otherwise read the
step's "running tests" phrase for a change that is actually parked waiting for a
slot (all parallel changes looked like they were running tests). waiting_for_test_lane
overrides the footer verb; test_lane_acquired or a pause restores it."""
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
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
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
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


_T = "2026-01-01T00:00:{:02d}+00:00"


def _paused_mid_test_lane_wait(*, replay: bool) -> nv.NarrativeBuilder:
    """step_started -> waiting_for_test_lane -> interrupt pause -> re-emitted step_started."""
    b = nv.NarrativeBuilder()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="integration-tests", kind="script",
           phase="integration", label="running tests", timestamp=_T.format(0)),
        ev("3", "waiting_for_test_lane", step="integration-tests", timestamp=_T.format(10)),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
           timestamp=_T.format(20)),
    ]
    if replay:                          # live clients never receive workflow_resumed
        events.append(ev("5", "workflow_resumed", in_process=True, timestamp=_T.format(30)))
    events.append(ev("6", "step_started", step="integration-tests", kind="script",
                     phase="integration", label="running tests", timestamp=_T.format(31)))
    for e in events:
        b.add(e)
    return b


def test_pause_ends_test_lane_wait_live() -> None:
    act = _paused_mid_test_lane_wait(replay=False).current_activity()
    assert act is not None
    assert act.verb == "running tests"
    assert act.lane_wait is False


def test_pause_ends_test_lane_wait_replay() -> None:
    act = _paused_mid_test_lane_wait(replay=True).current_activity()
    assert act is not None
    assert act.verb == "running tests"
    assert act.lane_wait is False


def test_contended_resume_restamps_test_lane_wait() -> None:
    b = _paused_mid_test_lane_wait(replay=False)
    b.add(ev("7", "waiting_for_test_lane", step="integration-tests", timestamp=_T.format(40)))
    act = b.current_activity()
    assert act is not None
    assert act.verb == "waiting to test"
    assert act.start_ts == _T.format(40)


def test_pause_ends_merge_lane_wait_and_next_wait_restamps() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="wait-for-head", kind="branch",
           phase="integrate", label="waiting for head", timestamp=_T.format(0)),
        ev("3", "lane_waiting", ahead=1, timestamp=_T.format(10)),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
           timestamp=_T.format(20)),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None and act.lane_wait is False
    b.add(ev("5", "step_started", step="wait-for-head", kind="branch",
             phase="integrate", label="waiting for head", timestamp=_T.format(31)))
    b.add(ev("6", "lane_waiting", ahead=1, timestamp=_T.format(40)))
    act = b.current_activity()
    assert act is not None
    assert act.verb == "waiting to merge (1 ahead)"
    assert act.lane_wait is True
    assert act.start_ts == _T.format(40)   # restamped, not the pre-pause 10s
