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
    assert ei.is_suppressed({"type": "agent_message_usage_patch"}) is True
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


def test_step_windows_spans_a_reincarnation() -> None:
    # A reincarnation re-enters a still-open step: both attempts are one window.
    events = [
        _ev(None, "step_started", step="coder", phase="build"),   # 1
        _ev("a", "tool_call", agent_id="step:build/coder"),       # 2  dead attempt
        _ev(None, "reincarnating", reason="context threshold crossed"),  # 3
        _ev(None, "step_started", step="coder", phase="build"),   # 4
        _ev("b", "tool_call", agent_id="step:build/coder"),       # 5  reborn attempt
        _ev(None, "step_completed", step="coder"),                # 6
    ]
    steps = ei.step_windows(_indexed(events))
    assert [s.name for s in steps] == ["coder"]
    assert steps[0].windows == [(1, 6)]
    assert steps[0].occurrences() == 1


def test_step_windows_separates_a_later_rerun_from_a_reincarnation() -> None:
    # A start AFTER the step completed still opens its own window.
    events = [
        _ev(None, "step_started", step="coder"),     # 1
        _ev(None, "step_started", step="coder"),     # 2  re-entry, step still open
        _ev(None, "step_completed", step="coder"),   # 3
        _ev(None, "step_started", step="review"),    # 4
        _ev(None, "step_completed", step="review"),  # 5
        _ev(None, "step_started", step="coder"),     # 6  genuine repeat
        _ev(None, "step_completed", step="coder"),   # 7
    ]
    steps = {s.name: s for s in ei.step_windows(_indexed(events))}
    assert steps["coder"].windows == [(1, 3), (6, 7)]
    assert steps["coder"].occurrences() == 2
    assert steps["review"].windows == [(4, 5)]


def test_build_step_index_scopes_a_reincarnated_step_to_both_attempts() -> None:
    # The consumer behind `imp events --step`: the dead attempt must be in scope.
    events = [
        _ev(None, "step_started", step="coder", phase="build"),   # 1
        _ev("a", "tool_call", agent_id="step:build/coder"),       # 2  dead attempt
        _ev(None, "reincarnating", reason="context threshold crossed"),  # 3
        _ev(None, "step_started", step="coder", phase="build"),   # 4
        _ev("b", "tool_call", agent_id="step:build/coder"),       # 5  reborn attempt
        _ev(None, "step_completed", step="coder"),                # 6
    ]
    si = ei.build_step_index(_indexed(events), "coder", {})
    assert si is not None
    assert si.windows == [(1, 6)]
    assert ei.in_intervals(2, si.windows) is True
    roster = {a.handle: a for a in si.agents}
    assert roster["coder"].ranges == [(1, 6)]
    assert roster["coder"].count == 6


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
        {"type": "agent_message_usage_patch", "agent_id": "step:build/coder"},  # 7 (suppressed)
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
    assert dict(idx.types).get("agent_message_usage_patch") is None


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
        _ev("f1", "agent_result", agent_id="step:main/task-lead"),     # 5 own
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


def test_agent_display_strips_step_and_resolves_handle() -> None:
    handles = {"sub:D1": "coder-1"}
    assert ei.agent_display("step:design/spec", handles) == "spec"
    assert ei.agent_display("sub:D1", handles) == "coder-1"
    assert ei.agent_display("sub:UNKNOWN", handles) == "sub:UNKNOWN"
    assert ei.agent_display(None, handles) == ""


def test_digest_line_format_ordinal_type_agent_no_timestamp() -> None:
    handles = {"sub:D1": "coder-1"}
    ev = _ev("a", "tool_call", agent_id="step:design/spec",
             timestamp="2026-07-01T00:00:00+00:00",
             name="read_file", input={"path": "src/imp/cli/main.py"})
    line = ei.digest_line(42, ev, handles)
    assert line.startswith("#42")
    assert "tool_call" in line
    assert "[spec]" in line
    assert "read_file" in line
    # the ordinal conveys order — the wall-clock timestamp never appears.
    assert "2026-07-01T00:00:00+00:00" not in line


def test_digest_line_blank_agent_for_none_author() -> None:
    line = ei.digest_line(3, _ev(None, "step_started", step="spec"), {})
    assert line.startswith("#3")
    assert "[]" in line


def _selection_fixture() -> list[dict[str, Any]]:
    return [
        _ev(None, "step_started", step="task-lead"),                    # 1
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),        # 2
        _ev("e1", "tool_call", agent_id="sub:D1"),                      # 3
        _ev("e2", "agent_result", agent_id="sub:D1"),                   # 4
        _ev(None, "step_completed", step="task-lead"),                  # 5
        _ev(None, "step_started", step="review"),                       # 6
        _ev("r1", "tool_call", agent_id="step:review/review"),         # 7
        {"type": "agent_message_usage_patch", "agent_id": "sub:D1"},      # 8 suppressed
        _ev(None, "step_completed", step="review"),                     # 9
    ]


def test_select_by_type_intersects_across_options() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    got = ei.select_event_lines(
        _indexed(events), types={"tool_call"}, handle_map=handles)
    assert [o for o, _ in got] == [2, 3, 7]


def test_select_by_agent_handle_and_step_name() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    # subagent handle resolves to its sub:<id> author.
    by_handle = ei.select_event_lines(
        _indexed(events), agents={"coder-1"}, handle_map=handles)
    assert [o for o, _ in by_handle] == [3, 4]
    # a step name matches that agent's own turns.
    by_step = ei.select_event_lines(
        _indexed(events), agents={"task-lead"}, handle_map=handles)
    assert [o for o, _ in by_step] == [2]


def test_select_type_and_agent_intersect() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    got = ei.select_event_lines(
        _indexed(events), types={"tool_call"}, agents={"coder-1"},
        handle_map=handles)
    assert [o for o, _ in got] == [3]


def test_select_by_range_and_step_window() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    by_range = ei.select_event_lines(
        _indexed(events), ranges=[(3, 4)], handle_map=handles)
    assert [o for o, _ in by_range] == [3, 4]
    # --step scopes to the window; suppressed line 8 never appears.
    by_step_window = ei.select_event_lines(
        _indexed(events), step_names={"review"}, all_=True, handle_map=handles)
    assert [o for o, _ in by_step_window] == [6, 7, 9]


def test_select_limit_keeps_last_n_and_excludes_suppressed() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    got = ei.select_event_lines(
        _indexed(events), all_=True, limit=2, handle_map=handles)
    # last 2 displayable rows are ordinals 7 and 9 (line 8 suppressed).
    assert [o for o, _ in got] == [7, 9]


def test_select_agent_selector_is_exact_not_substring() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    # a step-name / handle PREFIX must NOT substring-match a full handle.
    assert ei.select_event_lines(
        _indexed(events), agents={"task"}, handle_map=handles) == []
    assert ei.select_event_lines(
        _indexed(events), agents={"coder"}, handle_map=handles) == []
    # but a raw sub:<id> selector DOES substring-match (partial uuid support).
    partial = ei.select_event_lines(
        _indexed(events), agents={"sub:D"}, handle_map=handles)
    assert [o for o, _ in partial] == [3, 4]


def test_select_type_or_within_multivalue_set() -> None:
    events = _selection_fixture()
    handles = ei.subagent_handle_map(_indexed(events))
    # OR within the types set: tool_call (2,3,7) OR agent_result (4).
    got = ei.select_event_lines(
        _indexed(events), types={"tool_call", "agent_result"},
        handle_map=handles)
    assert [o for o, _ in got] == [2, 3, 4, 7]


def test_index_json_carries_steps_and_types() -> None:
    events = [
        _ev(None, "step_started", step="spec"),
        _ev("a", "tool_call", agent_id="step:design/spec"),
        _ev(None, "step_completed", step="spec"),
    ]
    idx = ei.build_index(_indexed(events), quest_id="q1")
    obj = ei.index_json(idx)
    assert obj["quest_id"] == "q1"
    assert obj["steps"][0]["name"] == "spec"
    assert obj["steps"][0]["ranges"] == [[1, 3]]
    assert isinstance(obj["types"], list)
    assert obj["types"][0]["type"] == "step_started"
    assert isinstance(obj["types"][0]["count"], int)


def test_events_json_includes_ordinal_and_full_payload() -> None:
    ev = _ev("a", "tool_call", agent_id="step:design/spec",
             timestamp="2026-07-01T00:00:00+00:00",
             name="read_file", input={"path": "x.py"})
    obj = ei.events_json([(42, ev)])
    assert obj[0]["ordinal"] == 42
    assert obj[0]["type"] == "tool_call"
    # full payload retains the wall-clock timestamp (unlike the digest line).
    assert obj[0]["timestamp"] == "2026-07-01T00:00:00+00:00"
    assert obj[0]["payload"]["name"] == "read_file"


def test_digest_line_clips_assembled_line_to_width() -> None:
    # Content is deliberately short (~30 chars) so it survives the per-field cap
    # still present until Task 3, while the assembled `#N  <type>  [agent]  …`
    # line (~85 chars) still exceeds the 40-column clip width.
    ev = _ev("a", "tool_result", agent_id="step:x/y",
             content="Z" * 30 + "TAILMARK", error=False)
    clipped = ei.digest_line(7, ev, {}, width=40)
    full = ei.digest_line(7, ev, {})            # default: no clip
    assert len(clipped) == 40                   # exactly one row's worth
    assert clipped.endswith("…")
    assert "TAILMARK" not in clipped            # the tail fell off the right edge
    assert "TAILMARK" in full                   # no-clip keeps the whole line
    assert len(full) > 40


def test_digest_line_no_clip_when_width_none() -> None:
    # 80 chars < the 120-char per-field cap still present until Task 3, so the
    # content passes through untouched under the no-clip (width=None) path.
    ev = _ev("a", "tool_result", agent_id="step:x/y",
             content="Q" * 80, error=False)
    line = ei.digest_line(1, ev, {}, width=None)
    assert "Q" * 80 in line
    assert not line.endswith("…")


def test_digest_line_width_larger_than_line_is_untouched() -> None:
    # Models the piped no-wrap width (10_000): long-but-under-width lines stay
    # whole so `grep` over piped output sees full lines.
    ev = _ev("a", "step_started", agent_id=None, step="spec")
    line = ei.digest_line(2, ev, {}, width=10_000)
    assert not line.endswith("…")
    assert "step=spec" in line


def test_select_filters_by_subtype():
    events = [
        {"type": "user_message", "agent_id": None,
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "go"}]}},
        {"type": "engine_message", "agent_id": "step:build/plan",
         "payload": {"role": "user", "subtype": "system_prompt",
                     "content": [{"type": "text", "text": "sys"}]}},
    ]
    indexed = list(enumerate(events, start=1))
    got = ei.select_event_lines(indexed, subtypes={"system_prompt"}, handle_map={})
    assert [o for o, _ in got] == [2]


def test_step_index_json_carries_windows_agents_and_types() -> None:
    events = [
        _ev(None, "step_started", step="task-lead"),                    # 1
        _ev("D1", "tool_call", agent_id="step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),        # 2
        _ev("e1", "tool_call", agent_id="sub:D1"),                      # 3
        _ev(None, "step_completed", step="task-lead"),                  # 4
    ]
    handles = ei.subagent_handle_map(_indexed(events))
    si = ei.build_step_index(_indexed(events), "task-lead", handles)
    obj = ei.step_index_json(si)
    assert obj["step"] == "task-lead"
    assert obj["windows"] == [[1, 4]]
    handles_out = {a["handle"] for a in obj["agents"]}
    assert "task-lead" in handles_out
    assert "coder-1" in handles_out
    assert isinstance(obj["types"], list)
    assert obj["types"][0]["type"]
    assert isinstance(obj["types"][0]["count"], int)


def test_usage_patch_overlays_key_the_preceding_same_agent_message() -> None:
    events = [
        _ev("m1", "agent_message", agent_id="sub:D1"),                   # 1
        _ev("m2", "agent_message", agent_id="step:build/coder"),         # 2
        # Persisted raw, exactly as state.update_last_agent_message_usage
        # writes it: no id, no timestamp.
        {"type": "agent_message_usage_patch", "agent_id": "sub:D1",
         "payload": {"usage": {"input_tokens": 7}, "context_tokens": 22_389,
                     "context_window": 500_000}},                        # 3
    ]
    assert ei.usage_patch_overlays(_indexed(events)) == {
        1: {"context_tokens": 22_389, "context_window": 500_000}}


def test_usage_patch_overlays_ignore_a_patch_with_no_preceding_message() -> None:
    events = [
        {"type": "agent_message_usage_patch", "agent_id": "sub:D1",
         "payload": {"usage": {}, "context_tokens": 10, "context_window": 20}},
    ]
    assert ei.usage_patch_overlays(_indexed(events)) == {}


def test_digest_line_overlay_renders_ctx_without_mutating_the_event() -> None:
    ev = _ev("m1", "agent_message", agent_id="step:build/coder",
             provider="codex", model="gpt-5.6", stop_reason="end_turn",
             usage={"input_tokens": 10, "output_tokens": 2},
             raw={"content": [{"type": "text", "text": "done"}]})
    line = ei.digest_line(
        5, ev, {}, overlay={"context_tokens": 136_000,
                            "context_window": 272_000})
    assert "ctx=136000/272000 (50.0%)" in line
    assert "context_tokens" not in ev["payload"]
    assert "ctx=" not in ei.digest_line(5, ev, {})
