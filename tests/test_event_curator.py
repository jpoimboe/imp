from __future__ import annotations

from imp.remote.curate import EventCurator


def test_curate_augments_allowed_event_with_change_context() -> None:
    curator = EventCurator(id="cid", title="My Change", events=("all",))
    out = curator.curate(
        {"id": "ev1", "type": "agent_output", "payload": {"text": "hi"}})
    assert out is not None
    assert out["id"] == "ev1"             # the event's OWN id is not clobbered
    assert out["type"] == "agent_output"
    assert out["change"] == {
        "id": "cid", "title": "My Change", "step": None, "status": "running"}


def test_curate_tracks_step_and_status_across_events() -> None:
    curator = EventCurator(id="cid", title="T", events=("all",))
    curator.curate({"type": "step_started", "payload": {"step": "plan"}})
    out = curator.curate({"type": "workflow_paused", "payload": {"reason": "x"}})
    assert out is not None
    assert out["change"]["step"] == "plan"
    assert out["change"]["status"] == "paused"


def test_curate_drops_type_not_on_allowlist() -> None:
    curator = EventCurator(id="cid", title="T", events=("awaiting_user_input",))
    assert curator.curate({"type": "tool_call", "payload": {}}) is None


def test_curate_tracks_dropped_events_so_later_allowed_event_reports_state() -> None:
    # A non-allowlisted step_started must STILL advance tracked step, so a later
    # allowed event carries the updated context: tracking precedes the allowlist.
    curator = EventCurator(id="cid", title="T", events=("awaiting_user_input",))
    assert curator.curate(
        {"type": "step_started", "payload": {"step": "plan"}}) is None
    out = curator.curate(
        {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
    assert out is not None
    assert out["change"]["step"] == "plan"


def test_curate_non_dict_returns_none() -> None:
    curator = EventCurator(id="cid", title="T", events=("all",))
    assert curator.curate("not-an-event") is None


def test_curate_tracks_merge_blocked_status() -> None:
    curator = EventCurator(id="cid", title="T", events=("all",))
    out = curator.curate({"type": "merge_blocked", "payload": {}})
    assert out is not None
    assert out["change"]["status"] == "merge_blocked"


def test_curate_tracks_change_merged_status() -> None:
    curator = EventCurator(id="cid", title="T", events=("all",))
    out = curator.curate({"type": "change_merged", "payload": {}})
    assert out is not None
    assert out["change"]["status"] == "completed"
