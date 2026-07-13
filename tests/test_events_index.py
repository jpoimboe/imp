from __future__ import annotations

from typing import Any

from imp.engine import events_index as ei


def _ev(
    eid: str | None,
    etype: str,
    agent_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """An events.jsonl-shaped event dict. A `timestamp` kwarg is hoisted to the
    top-level field; everything else goes in `payload`."""
    ts = payload.pop("timestamp", None)
    event: dict[str, Any] = {
        "id": eid,
        "parent_id": None,
        "agent_id": agent_id,
        "type": etype,
        "payload": payload,
    }
    if ts is not None:
        event["timestamp"] = ts
    return event


def _indexed(events: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return list(enumerate(events, start=1))


def test_is_suppressed_flags_malformed_and_usage_patch() -> None:
    assert ei.is_suppressed({"_malformed": True, "raw": "{bad"}) is True
    assert ei.is_suppressed({"type": "llm_response_usage_patch"}) is True
    assert ei.is_suppressed({"type": "tool_call"}) is False


def test_coalesce_ranges_merges_adjacent_and_splits_gaps() -> None:
    assert ei.coalesce_ranges([3, 1, 2, 5, 6, 9]) == [(1, 3), (5, 6), (9, 9)]
    assert ei.coalesce_ranges([]) == []
    assert ei.coalesce_ranges([4, 4, 4]) == [(4, 4)]


def test_format_ranges_uses_hash_prefix_and_dash() -> None:
    assert ei.format_ranges([(59, 120), (210, 255)]) == "#59-120, #210-255"
    assert ei.format_ranges([(42, 42)]) == "#42"


def test_parse_range_spec_single_list_and_ranges() -> None:
    assert ei.parse_range_spec("42", max_ordinal=500) == [(42, 42)]
    assert ei.parse_range_spec("42,55,60", max_ordinal=500) == [
        (42, 42), (55, 55), (60, 60)]
    assert ei.parse_range_spec("59-120,210-255", max_ordinal=500) == [
        (59, 120), (210, 255)]


def test_parse_range_spec_open_ends_resolve_against_max() -> None:
    assert ei.parse_range_spec("59-", max_ordinal=300) == [(59, 300)]
    assert ei.parse_range_spec("-80", max_ordinal=300) == [(1, 80)]


def test_parse_range_spec_tolerates_hash_and_whitespace() -> None:
    assert ei.parse_range_spec(" #59-120 , #210-255 ", max_ordinal=999) == [
        (59, 120), (210, 255)]


def test_in_intervals_membership() -> None:
    intervals = [(59, 120), (210, 255)]
    assert ei.in_intervals(59, intervals) is True
    assert ei.in_intervals(255, intervals) is True
    assert ei.in_intervals(200, intervals) is False


def test_format_ranges_round_trips_through_parse() -> None:
    ranges = [(59, 120), (210, 255)]
    text = ei.format_ranges(ranges)
    assert ei.parse_range_spec(text, max_ordinal=999) == ranges


def test_step_windows_records_ordinal_span_per_occurrence() -> None:
    events = [
        _ev(None, "step_started", step="spec", phase="design"),   # 1
        _ev("a", "tool_call", agent_id="step:design/spec"),        # 2
        _ev(None, "step_completed", step="spec"),                  # 3
        _ev(None, "step_started", step="coder", phase="build"),    # 4
        _ev("b", "tool_call", agent_id="step:build/coder"),        # 5
        _ev(None, "step_completed", step="coder"),                 # 6
    ]
    steps = ei.step_windows(_indexed(events))
    assert [s.name for s in steps] == ["spec", "coder"]
    assert steps[0].windows == [(1, 3)]
    assert steps[1].windows == [(4, 6)]
    assert steps[1].occurrences() == 1


def test_step_windows_collects_multiple_occurrences() -> None:
    events = [
        _ev(None, "step_started", step="coder"),     # 1
        _ev(None, "step_completed", step="coder"),   # 2
        _ev(None, "step_started", step="review"),    # 3
        _ev(None, "step_completed", step="review"),  # 4
        _ev(None, "step_started", step="coder"),     # 5
        _ev(None, "step_completed", step="coder"),   # 6
    ]
    steps = {s.name: s for s in ei.step_windows(_indexed(events))}
    assert steps["coder"].windows == [(1, 2), (5, 6)]
    assert steps["coder"].occurrences() == 2


def test_step_windows_closes_open_step_at_last_ordinal() -> None:
    events = [
        _ev(None, "step_started", step="coder"),            # 1
        _ev("a", "tool_call", agent_id="step:build/coder"),  # 2
        _ev("b", "tool_result", agent_id="step:build/coder"),# 3
    ]
    steps = ei.step_windows(_indexed(events))
    assert [s.name for s in steps] == ["coder"]
    # no step_completed — the open step closes at the last ordinal (3).
    assert steps[0].windows == [(1, 3)]
    assert steps[0].occurrences() == 1


def test_subagent_handle_map_numbers_globally_by_dispatch_order() -> None:
    # Each dispatch tool_call has a known event id (first `_ev` positional); the
    # subagent's agent_id is sub:<that id>. Numbering is a global per-step
    # counter across names, not per-name.
    events = [
        _ev(None, "step_started", step="task-lead"),                     # 1
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),         # 2
        _ev("e1", "tool_call", agent_id="sub:D1"),                       # 3
        _ev("D2", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),         # 4
        _ev("e2", "tool_call", agent_id="sub:D2"),                       # 5
        _ev("D3", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "code-reviewer"}), # 6
        _ev("e3", "tool_call", agent_id="sub:D3"),                       # 7
        _ev(None, "step_completed", step="task-lead"),                   # 8
    ]
    handles = ei.subagent_handle_map(_indexed(events))
    assert handles == {
        "sub:D1": "coder-1",
        "sub:D2": "coder-2",
        "sub:D3": "code-reviewer-3",
    }
