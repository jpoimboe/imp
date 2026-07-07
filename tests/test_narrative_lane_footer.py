from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _acquire_open() -> nv.NarrativeBuilder:
    """A builder with an open merge acquire step (no lane_waiting yet)."""
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue"))
    return b


def test_first_lane_waiting_appends_one_node_and_sets_live_label() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    nodes = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert len(nodes) == 1
    assert nodes[0].text == "waiting to merge · 3 ahead"
    assert step.lane_waiting_node is nodes[0]
    assert step.live_label == "waiting to merge · 3 ahead"
    assert b.current_activity().verb == "waiting to merge · 3 ahead"


def test_subsequent_lane_waiting_mutates_same_node_no_append() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    first_node = step.lane_waiting_node
    b.add(ev("4", "lane_waiting", base="main", ahead=1))
    nodes = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    # Still exactly one transcript node — the drain updated it in place.
    assert len(nodes) == 1
    assert nodes[0] is first_node
    assert first_node.text == "waiting to merge · 1 ahead"
    assert step.live_label == "waiting to merge · 1 ahead"
    assert b.current_activity().verb == "waiting to merge · 1 ahead"


def test_lane_acquired_clears_live_label_and_node_ref() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    assert step.live_label == "waiting to merge · 3 ahead"
    assert step.lane_waiting_node is not None
    b.add(ev("4", "lane_acquired", base="main"))
    # Footer reverts to the step's own label; the per-step node ref is dropped.
    assert step.live_label is None
    assert step.lane_waiting_node is None
    assert b.current_activity().verb == "waiting in merge queue"


def test_lane_released_still_uses_generic_handler() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    # A release event must NOT touch the footer override (only lane_acquired does).
    b.add(ev("4", "lane_released", base="main"))
    assert step.live_label == "waiting to merge · 3 ahead"
    assert step.lane_waiting_node is not None


def test_lane_evicted_still_uses_generic_handler() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    # An evict event must NOT touch the footer override (only lane_acquired does).
    b.add(ev("4", "lane_evicted", base="main"))
    assert step.live_label == "waiting to merge · 3 ahead"
    assert step.lane_waiting_node is not None
