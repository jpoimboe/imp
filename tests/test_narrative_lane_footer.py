from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _acquire_open() -> nv.NarrativeBuilder:
    """A builder with an open merge acquire step (no lane_waiting yet)."""
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue"))
    return b


def _wait_nodes(step: nv.StepNode) -> list[nv.LiteralTextNode]:
    """Scrollback nodes for the merge wait — must always be empty (footer-only)."""
    return [c for c in step.children
            if isinstance(c, nv.LiteralTextNode)
            and c.text.startswith("waiting to merge")]


def test_lane_waiting_appends_no_scrollback_node() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    # AC1: no transcript line for the wait — footer-only.
    assert _wait_nodes(step) == []
    # AC2: footer state is set from the event.
    assert step.lane_wait_label == "waiting to merge (3 ahead)"
    assert step.lane_wait is True
    assert b.current_activity().verb == "waiting to merge (3 ahead)"
    assert b.current_activity().lane_wait is True


def test_successive_lane_waiting_updates_footer_in_place() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3,
             timestamp="2026-06-03T00:00:03+00:00"))
    step = b._step_stack[-1]
    # Queue-entry clock stamped on the FIRST wait.
    assert step.lane_wait_start_ts == "2026-06-03T00:00:03+00:00"

    b.add(ev("4", "lane_waiting", base="main", ahead=1,
             timestamp="2026-06-03T00:00:09+00:00"))
    # AC2: count drops in the footer, still no scrollback node.
    assert _wait_nodes(step) == []
    assert step.lane_wait_label == "waiting to merge (1 ahead)"
    assert b.current_activity().verb == "waiting to merge (1 ahead)"
    # Timer stays pinned to the first wait — a later event does not restamp it.
    assert step.lane_wait_start_ts == "2026-06-03T00:00:03+00:00"


def test_lane_acquired_reverts_footer_to_step_label() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    assert step.lane_wait_label == "waiting to merge (3 ahead)"

    b.add(ev("4", "lane_acquired", base="main"))
    # AC3: footer reverts to the step's own label; no leftover scrollback line.
    assert step.lane_wait_label is None
    assert step.lane_wait is False
    assert _wait_nodes(step) == []
    assert b.current_activity().verb == "waiting in merge queue"
    assert b.current_activity().lane_wait is False


def test_lane_released_does_not_touch_footer_override() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    # AC4: a release event must NOT clear the footer override (only lane_acquired does).
    b.add(ev("4", "lane_released", base="main"))
    assert step.lane_wait_label == "waiting to merge (3 ahead)"
    assert step.lane_wait is True


def test_lane_evicted_does_not_touch_footer_override() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    # AC4: an evict event must NOT clear the footer override (only lane_acquired does).
    b.add(ev("4", "lane_evicted", base="main"))
    assert step.lane_wait_label == "waiting to merge (3 ahead)"
    assert step.lane_wait is True


def test_resume_reentry_leaves_footer_ready() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]

    # Resume continuation (restarted=False): the re-emitted step_started reopens
    # the still-open acquire step in place.
    b.add(ev("4", "workflow_resumed"))
    b.add(ev("5", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue", restarted=False))
    reopened = b._step_stack[-1]
    assert reopened is step

    # AC5: the next wait renders cleanly — footer set, still no scrollback node.
    b.add(ev("6", "lane_waiting", base="main", ahead=2))
    assert _wait_nodes(reopened) == []
    assert reopened.lane_wait_label == "waiting to merge (2 ahead)"
    assert b.current_activity().verb == "waiting to merge (2 ahead)"


def test_retry_reentry_leaves_footer_ready() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]

    # Retry re-entry (resumed=True, NO prior workflow_resumed): the transient-error
    # retry loop re-emits step_started for the still-open step.
    b.add(ev("4", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue", resumed=True))
    reopened = b._step_stack[-1]
    assert reopened is step

    b.add(ev("5", "lane_waiting", base="main", ahead=1))
    assert _wait_nodes(reopened) == []
    assert reopened.lane_wait_label == "waiting to merge (1 ahead)"
    assert b.current_activity().verb == "waiting to merge (1 ahead)"


def test_restarted_reentry_leaves_footer_ready() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]

    # Genuine re-run (restarted=True): the shared reopen wipes children; the next
    # wait must still render cleanly.
    b.add(ev("4", "workflow_resumed"))
    b.add(ev("5", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue", restarted=True))
    reopened = b._step_stack[-1]
    assert reopened is step

    b.add(ev("6", "lane_waiting", base="main", ahead=1))
    assert _wait_nodes(reopened) == []
    assert reopened.lane_wait_label == "waiting to merge (1 ahead)"
    assert b.current_activity().verb == "waiting to merge (1 ahead)"


def test_lane_wait_toggles_through_merge_lane_handlers() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=2))
    assert b.current_activity().lane_wait is True
    b.add(ev("4", "lane_acquired", base="main"))
    assert b.current_activity().lane_wait is False
