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


def test_build_index_step_ranges_counts_and_types_histogram() -> None:
    events = [
        _ev(None, "step_started", step="spec"),                # 1
        _ev("a", "tool_call", agent_id="step:design/spec"),     # 2
        _ev("b", "tool_result", agent_id="step:design/spec"),   # 3
        _ev(None, "step_completed", step="spec"),               # 4
        _ev(None, "step_started", step="coder"),                # 5
        _ev("c", "tool_call", agent_id="step:build/coder"),     # 6
        {"type": "llm_response_usage_patch", "agent_id": "step:build/coder"},  # 7 (suppressed)
        _ev(None, "step_completed", step="coder"),              # 8
    ]
    idx = ei.build_index(_indexed(events), quest_id="q1")
    assert idx.quest_id == "q1"
    assert idx.step_count == 2
    # 7 displayable events (line 7 suppressed).
    assert idx.total_events == 7
    rows = {r.name: r for r in idx.steps}
    assert rows["spec"].ranges == [(1, 4)]
    assert rows["spec"].count == 4
    assert rows["coder"].ranges == [(5, 8)]
    assert rows["coder"].count == 3  # line 7 excluded from the count
    # types histogram is count-descending; suppressed type absent.
    assert idx.types[0] == ("step_started", 2)
    assert dict(idx.types).get("llm_response_usage_patch") is None


def test_build_index_marks_loop_and_subagent_fanout() -> None:
    events = [
        _ev(None, "step_started", step="task-lead"),        # 1
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),  # 2
        _ev("e1", "tool_call", agent_id="sub:D1"),          # 3
        _ev(None, "step_completed", step="task-lead"),      # 4
        _ev(None, "step_started", step="task-lead"),        # 5
        _ev(None, "step_completed", step="task-lead"),      # 6
    ]
    idx = ei.build_index(_indexed(events), quest_id="q1")
    row = {r.name: r for r in idx.steps}["task-lead"]
    assert row.occurrences == 2
    assert row.ranges == [(1, 4), (5, 6)]
    assert row.subagents == 1


def test_render_index_shows_header_ranges_loop_and_fanout() -> None:
    events = [
        _ev(None, "step_started", step="task-lead"),        # 1
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),  # 2
        _ev("e1", "tool_call", agent_id="sub:D1"),          # 3
        _ev(None, "step_completed", step="task-lead"),      # 4
        _ev(None, "step_started", step="task-lead"),        # 5
        _ev(None, "step_completed", step="task-lead"),      # 6
    ]
    idx = ei.build_index(_indexed(events), quest_id="q1")
    text = "\n".join(ei.render_index(idx))
    assert "quest q1 · 6 events · 1 steps" in text
    assert "steps:" in text
    assert "#1-4, #5-6" in text
    assert "(×2)" in text
    assert "↳ 1 subagent" in text
    assert "types:" in text
    assert "step_started" in text
    assert "→ --step" in text


def test_build_step_index_tiles_window_across_own_agent_and_subagents() -> None:
    # task-lead owns the gaps; coder-1 owns its contiguous block.
    events = [
        _ev(None, "step_started", step="task-lead"),                    # 1 own
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),        # 2 own
        _ev("e1", "tool_call", agent_id="sub:D1"),                      # 3 coder-1
        _ev("e2", "tool_result", agent_id="sub:D1"),                   # 4 coder-1
        _ev("f1", "agent_output", agent_id="step:main/task-lead"),     # 5 own
        _ev(None, "step_completed", step="task-lead"),                  # 6 own
    ]
    handles = ei.subagent_handle_map(_indexed(events))
    si = ei.build_step_index(_indexed(events), "task-lead", handles)
    assert si is not None
    assert si.step_name == "task-lead"
    assert si.windows == [(1, 6)]
    roster = {a.handle: a for a in si.agents}
    # own agent is gappy: two segments around the subagent block.
    assert roster["task-lead"].ranges == [(1, 2), (5, 6)]
    assert roster["task-lead"].count == 4
    # subagent is one contiguous run.
    assert roster["coder-1"].ranges == [(3, 4)]
    assert roster["coder-1"].count == 2
    # own agent listed first.
    assert si.agents[0].handle == "task-lead"


def test_build_step_index_unknown_step_returns_none() -> None:
    si = ei.build_step_index(_indexed([_ev(None, "step_started", step="spec"),
                                       _ev(None, "step_completed", step="spec")]),
                             "nope", {})
    assert si is None


def test_render_step_index_shows_window_agents_and_types() -> None:
    events = [
        _ev(None, "step_started", step="task-lead"),                    # 1
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),        # 2
        _ev("e1", "tool_call", agent_id="sub:D1"),                      # 3
        _ev(None, "step_completed", step="task-lead"),                  # 4
    ]
    handles = ei.subagent_handle_map(_indexed(events))
    si = ei.build_step_index(_indexed(events), "task-lead", handles)
    text = "\n".join(ei.render_step_index(si))
    assert "step task-lead · #1-4 · 4 events" in text
    assert "agents:" in text
    assert "task-lead" in text
    assert "coder-1" in text
    assert "types:" in text


def test_build_step_index_covers_multiple_windows_for_looped_step() -> None:
    events = [
        _ev(None, "step_started", step="task-lead"),        # 1
        _ev("a", "tool_call", agent_id="step:main/task-lead"),  # 2
        _ev(None, "step_completed", step="task-lead"),      # 3
        _ev(None, "step_started", step="review"),           # 4
        _ev(None, "step_completed", step="review"),         # 5
        _ev(None, "step_started", step="task-lead"),        # 6
        _ev("b", "tool_call", agent_id="step:main/task-lead"),  # 7
        _ev(None, "step_completed", step="task-lead"),      # 8
    ]
    handles = ei.subagent_handle_map(_indexed(events))
    si = ei.build_step_index(_indexed(events), "task-lead", handles)
    assert si is not None
    # both occurrences' windows are captured, in order.
    assert si.windows == [(1, 3), (6, 8)]
    assert si.total_events == 6  # the review step's 2 events are outside the window
    roster = {a.handle: a for a in si.agents}
    assert roster["task-lead"].ranges == [(1, 3), (6, 8)]
    assert roster["task-lead"].count == 6
    text = "\n".join(ei.render_step_index(si))
    assert "#1-3, #6-8" in text
