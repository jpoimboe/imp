from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _acquire_open() -> nv.NarrativeBuilder:
    """A builder with an open merge acquire step (no lane_waiting yet)."""
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue"))
    return b


def test_first_lane_waiting_appends_one_node_and_sets_live_label() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    nodes = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert len(nodes) == 1
    assert nodes[0].text == "waiting to merge (3 ahead)"
    assert step.lane_waiting_node is nodes[0]
    assert step.live_label == "waiting to merge (3 ahead)"
    assert b.current_activity().verb == "waiting to merge (3 ahead)"


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
    assert first_node.text == "waiting to merge (1 ahead)"
    assert step.live_label == "waiting to merge (1 ahead)"
    assert b.current_activity().verb == "waiting to merge (1 ahead)"


def test_lane_acquired_clears_live_label_and_node_ref() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    assert step.live_label == "waiting to merge (3 ahead)"
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
    assert step.live_label == "waiting to merge (3 ahead)"
    assert step.lane_waiting_node is not None


def test_lane_evicted_still_uses_generic_handler() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    # An evict event must NOT touch the footer override (only lane_acquired does).
    b.add(ev("4", "lane_evicted", base="main"))
    assert step.live_label == "waiting to merge (3 ahead)"
    assert step.lane_waiting_node is not None


def test_resume_reentry_clears_node_ref_so_next_wait_starts_fresh() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    stale_node = step.lane_waiting_node
    assert stale_node is not None

    # Resume opens the re-entry window; the re-emitted step_started reopens the
    # still-open acquire step in place (continuation: restarted=False).
    b.add(ev("4", "workflow_resumed"))
    b.add(ev("5", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue", restarted=False))
    reopened = b._step_stack[-1]
    assert reopened is step                     # reused in place, not a fresh node
    assert reopened.lane_waiting_node is None    # stale ref dropped on re-entry

    # The next lane_waiting appends a NEW transcript node, not a mutation of the
    # pre-interrupt one.
    b.add(ev("6", "lane_waiting", base="main", ahead=2))
    assert reopened.lane_waiting_node is not None
    assert reopened.lane_waiting_node is not stale_node
    assert reopened.lane_waiting_node.text == "waiting to merge (2 ahead)"


def test_retry_reentry_clears_node_ref_so_next_wait_starts_fresh() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    stale_node = step.lane_waiting_node
    assert stale_node is not None

    # Retry re-entry (NOT a resume): the executor's transient-error retry loop
    # re-emits step_started with resumed=True for the still-open step. With no
    # workflow_resumed before it, _resuming is False, so the retry-re-entry
    # branch fires (disjoint from the resume path guarded by `not self._resuming`).
    b.add(ev("4", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue", resumed=True))
    reopened = b._step_stack[-1]
    assert reopened is step                      # reused in place
    assert reopened.lane_waiting_node is None     # stale ref dropped on retry re-entry

    b.add(ev("5", "lane_waiting", base="main", ahead=1))
    assert reopened.lane_waiting_node is not None
    assert reopened.lane_waiting_node is not stale_node
    assert reopened.lane_waiting_node.text == "waiting to merge (1 ahead)"


def test_restarted_reentry_clears_node_ref_so_next_wait_starts_fresh() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=3))
    step = b._step_stack[-1]
    stale_node = step.lane_waiting_node
    assert stale_node is not None

    # Genuine re-run (restarted=True): the shared reopen wipes children and must
    # also drop the stale node ref so the next wait appends fresh.
    b.add(ev("4", "workflow_resumed"))
    b.add(ev("5", "step_started", step="acquire", kind="agent", phase="merge",
             label="waiting in merge queue", restarted=True))
    reopened = b._step_stack[-1]
    assert reopened is step
    assert reopened.lane_waiting_node is None

    b.add(ev("6", "lane_waiting", base="main", ahead=1))
    assert reopened.lane_waiting_node is not None
    assert reopened.lane_waiting_node is not stale_node
    assert reopened.lane_waiting_node.text == "waiting to merge (1 ahead)"


def test_lane_wait_toggles_through_merge_lane_handlers() -> None:
    b = _acquire_open()
    b.add(ev("3", "lane_waiting", base="main", ahead=2))
    assert b.current_activity().lane_wait is True
    b.add(ev("4", "lane_acquired", base="main"))
    assert b.current_activity().lane_wait is False
