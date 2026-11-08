"""The `paused · resumed <duration> later` marker, derived from the event log."""
from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_markdown as rm
from imp.cli import render_text as rt
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_html import render_html
from imp.cli.render_text import render_tree
from tests._event_fixtures import agent_text, ev

Events = list[dict[str, Any]]

CTRL_C = {"reason": "interrupt", "interrupt_reason": "ctrl-c"}
CTRL_D = {"reason": "interrupt", "interrupt_reason": "ctrl-d"}
MARKER = "paused · resumed"
MARKER_2M = "paused · resumed 2m later"
MARKER_2M45 = "paused · resumed 2m 45s later"
AID = "step:review/review"
AGENT = {"step": "review", "kind": "agent", "phase": "review", "label": "reviewing",
         "model": "m"}
SCRIPT = {"step": "verify", "kind": "script", "phase": "implement",
          "label": "running tests"}
COLLAPSED = "running tests · complete · 30s"
_DONE = {"action": {"verb": "continue"}, "var_mutations": {}}


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _at(secs: float) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=secs)).isoformat()


def _quest() -> dict[str, Any]:
    return ev("0", "quest_created", quest_id=1, title="x", timestamp=_at(0))


def _agent_run(pause: dict[str, Any] = CTRL_D, *, gap: float = 120,
               resumed: dict[str, Any] | None = None,
               restarted: bool = False) -> Events:
    return [_quest(),
            ev("1", "step_started", **AGENT, timestamp=_at(0)),
            agent_text("2", "before", agent_id=AID),
            ev("3", "workflow_paused", **pause, timestamp=_at(10)),
            ev("4", "workflow_resumed", **(resumed or {}), timestamp=_at(10 + gap)),
            ev("5", "step_started", **AGENT, resumed=True, restarted=restarted,
               timestamp=_at(10 + gap)),
            agent_text("6", "after", agent_id=AID),
            ev("7", "step_completed", step="review", kind="agent", **_DONE,
               timestamp=_at(20 + gap))]


def _script_run(*, pause: dict[str, Any] = CTRL_C, resumed: dict[str, Any] | None = None,
                before: tuple[str, ...] = (), after: tuple[str, ...] = (),
                **start: Any) -> Events:
    st = {**SCRIPT, **start}
    return [_quest(),
            ev("1", "step_started", **st, timestamp=_at(0)),
            *[ev(f"b{i}", "script_output", step="verify", stdout=s, timestamp=_at(5))
              for i, s in enumerate(before)],
            ev("2", "workflow_paused", **pause, timestamp=_at(10)),
            ev("3", "workflow_resumed", **(resumed or {}), timestamp=_at(175)),
            ev("4", "step_started", **st, timestamp=_at(175)),
            *[ev(f"a{i}", "script_output", step="verify", stdout=s, timestamp=_at(180))
              for i, s in enumerate(after)],
            ev("5", "step_completed", step="verify", kind="script", **_DONE,
               timestamp=_at(195))]


def _two_cycle_script() -> Events:
    return [_quest(),
            ev("1", "step_started", **SCRIPT, timestamp=_at(0)),
            ev("2", "workflow_paused", **CTRL_C, timestamp=_at(10)),
            ev("3", "workflow_resumed", timestamp=_at(175)),
            ev("4", "step_started", **SCRIPT, timestamp=_at(175)),
            ev("5", "workflow_paused", **CTRL_D, timestamp=_at(180)),
            ev("6", "workflow_resumed", timestamp=_at(240)),
            ev("7", "step_started", **SCRIPT, timestamp=_at(240)),
            ev("8", "step_completed", step="verify", kind="script", **_DONE,
               timestamp=_at(255))]


def _nested_script() -> Events:
    outer = {"step": "A", "kind": "script", "phase": "implement", "label": "step A"}
    inner = {"step": "B", "kind": "script", "phase": "implement", "label": "step B"}
    return [_quest(),
            ev("1", "step_started", **outer, timestamp=_at(0)),
            ev("2", "step_started", **inner, timestamp=_at(5)),
            ev("3", "workflow_paused", **CTRL_C, timestamp=_at(10)),
            ev("4", "workflow_resumed", timestamp=_at(175)),
            ev("5", "step_started", **inner, timestamp=_at(175)),
            ev("6", "step_completed", step="B", kind="script", **_DONE, timestamp=_at(190)),
            ev("7", "step_completed", step="A", kind="script", **_DONE, timestamp=_at(195))]


def _log(events: Events) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def _markers(events: Events) -> list[str]:
    return [ln for ln in _log(events) if ln.startswith(MARKER)]


def _live(events: Events) -> list[str]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def _phase(events: Events) -> nv.PhaseNode:
    phases = [c for c in nv.build_tree(events).root.children if isinstance(c, nv.PhaseNode)]
    return phases[-1]


# AC1
def test_leave_renders_a_spaced_marker_ahead_of_the_resume_header() -> None:
    lines = _log(_agent_run(CTRL_D, gap=120))
    i = lines.index(MARKER_2M)
    assert lines[i - 1] == "" and lines[i + 1] == ""
    assert not any("(resume)" in ln for ln in lines[:i])
    assert any("(resume)" in ln for ln in lines[i:])
    assert lines.index("• before") < i < lines.index("• after")


# AC1(b): held and show_output shapes, where no neighbour supplies the blank below.
def test_marker_stands_clear_of_a_collapsed_step() -> None:
    assert _log(_script_run()) == [
        "", "── implement phase ───", "", MARKER_2M45, "", COLLAPSED]


def test_marker_stands_clear_of_surfaced_script_output() -> None:
    lines = _log(_script_run(show_output=True, before=("out before",), after=("out after",)))
    i = lines.index(MARKER_2M45)
    assert lines[i - 2:i + 3] == ["• out before", "", MARKER_2M45, "", "• out after"]


# AC1(b): --full prints a fallback line, which asks for no blank below itself.
def test_marker_stands_clear_of_a_line_that_wants_no_space() -> None:
    events = _script_run(show_output=True)
    events.insert(2, ev("f", "unrecognized_event", timestamp=_at(5)))
    lines = list(render_tree(nv.build_tree(events), full=True, color=False, width=80))
    assert lines[lines.index(MARKER_2M45) - 2:lines.index(MARKER_2M45)] == [
        "unrecognized_event", ""]


def test_marker_node_asks_for_space_on_both_sides() -> None:
    node = nv.ResumeMarkerNode(gap_s=120)
    assert rt.spaces_before(node)
    assert rt.spaces_after(node, full=False)


# AC2(a)
@pytest.mark.parametrize("pause", [
    CTRL_D,
    *[{"reason": "interrupt", "interrupt_reason": r}
      for r in ("pause", "sigterm", "sighup", "sigquit", "detached")],
    *[{"reason": r}
      for r in ("awaiting_user", "quota", "reauth_needed", "max_loops", "internal_error")],
], ids=lambda p: p.get("interrupt_reason") or p["reason"])
def test_a_real_absence_gets_the_marker(pause: dict[str, Any]) -> None:
    assert _markers(_agent_run(pause)) == [MARKER_2M]


def test_an_unknown_reason_gets_the_marker() -> None:
    assert _markers(_agent_run({"reason": "brand_new_reason"})) == [MARKER_2M]


# AC2(b)
def test_awaiting_user_resume_gets_the_marker_and_no_resume_header() -> None:
    lines = _log(_agent_run({"reason": "awaiting_user"}))
    assert MARKER_2M in lines
    assert not any("(resume)" in ln for ln in lines)


# AC3(a)
@pytest.mark.parametrize("reason", ["interrupt", "model-override", "compact", "awaiting_turn"])
def test_a_non_absence_reason_gets_no_marker(reason: str) -> None:
    assert _markers(_agent_run({"reason": reason})) == []


# AC3(b)
def test_the_in_process_flag_suppresses_the_marker_whatever_the_reason() -> None:
    assert _markers(_agent_run({"reason": "quota"}, resumed={"in_process": True})) == []


# AC4(a)
def test_a_crash_resume_with_no_pause_gets_no_marker() -> None:
    events = [e for e in _agent_run() if e["type"] != "workflow_paused"]
    assert _markers(events) == []


# AC4(b)
@pytest.mark.parametrize("boundary", [
    ev("b", "step_started", **AGENT, timestamp=_at(50)),
    ev("b", "step_completed", step="review", kind="agent", **_DONE, timestamp=_at(50)),
], ids=["step_started", "step_completed"])
def test_a_step_boundary_closes_the_pause(boundary: dict[str, Any]) -> None:
    events = [_quest(),
              ev("1", "step_started", **AGENT, timestamp=_at(0)),
              ev("2", "workflow_paused", **CTRL_C, timestamp=_at(10)),
              boundary,
              ev("3", "workflow_resumed", timestamp=_at(500))]
    assert _markers(events) == []


# AC4(c)
def test_a_resume_consumes_the_pause() -> None:
    events = [_quest(),
              ev("1", "step_started", **AGENT, timestamp=_at(0)),
              ev("2", "workflow_paused", **CTRL_D, timestamp=_at(10)),
              ev("3", "workflow_resumed", timestamp=_at(130)),
              ev("4", "cli_attached", timestamp=_at(131)),
              ev("5", "workflow_resumed", timestamp=_at(500))]
    assert _markers(events) == [MARKER_2M]


# AC5(a)
@pytest.mark.parametrize(("gap", "text"), [
    (45, "45s"), (60, "1m"), (160, "2m 40s"), (235, "3m 55s"), (3600, "1h"),
    (7980, "2h 13m"), (172799, "47h 59m"), (48 * 3600, "2d"), (76 * 3600, "3d 4h"),
])
def test_duration_table(gap: int, text: str) -> None:
    assert _markers(_agent_run(gap=gap)) == [f"paused · resumed {text} later"]


# The duration floors to whole seconds.
@pytest.mark.parametrize(("gap", "text"), [
    (1.0, "1s"), (59.7, "59s"), (60.9, "1m"), (119.9, "1m 59s"),
])
def test_duration_floors(gap: float, text: str) -> None:
    assert _markers(_agent_run(gap=gap)) == [f"paused · resumed {text} later"]


# AC3: the resume recap's pause age shares these strings (test_narrative_interrupt_recap.py).
SHARED_DURATIONS: list[tuple[int, str]] = [
    (59, "59s"), (60, "1m"), (3 * 3600, "3h"), (2 * 3600 + 13 * 60, "2h 13m"),
    (49 * 3600, "2d 1h"),
]


@pytest.mark.parametrize(("gap", "text"), SHARED_DURATIONS)
def test_marker_durations_match_the_recap_age(gap: int, text: str) -> None:
    assert _markers(_agent_run(gap=gap)) == [f"paused · resumed {text} later"]


# AC5(b)
@pytest.mark.parametrize("gap", [0, 0.6, 0.999])
def test_a_gap_under_one_second_gets_no_marker(gap: float) -> None:
    assert _markers(_agent_run(gap=gap)) == []


# AC5(c)
def test_an_unparseable_timestamp_gets_no_marker() -> None:
    events = _agent_run()
    assert _markers([{**e, "timestamp": "not-a-time"} if e["type"] == "workflow_paused" else e
                     for e in events]) == []


# AC5(d)
def test_retry_wait_formatting_keeps_its_zero_units() -> None:
    assert nv._fmt_hm(3600) == "1h 0m"
    assert nv._fmt_hm(48 * 3600) == "2d 0h"
    assert nv.fmt_seconds(60) == "1m 0s"
    assert nv.fmt_seconds(3600) == "1h 0m"


# AC6(a)
def test_midturn_leave_measures_from_the_leave_pause() -> None:
    events = [_quest(),
              ev("1", "step_started", **AGENT, timestamp=_at(0)),
              ev("2", "interrupt_requested", source="ctrl-c", via="socket", timestamp=_at(0)),
              ev("3", "workflow_paused", **CTRL_D, timestamp=_at(300)),
              ev("4", "workflow_resumed", timestamp=_at(3600))]
    assert _markers(events) == ["paused · resumed 55m later"]


# AC6(b)
def test_hold_prompt_leave_measures_from_the_ctrl_c_pause() -> None:
    events = [_quest(),
              ev("1", "step_started", **AGENT, timestamp=_at(0)),
              ev("2", "workflow_paused", **CTRL_C, timestamp=_at(0)),
              ev("3", "workflow_resumed", timestamp=_at(3600))]
    assert _markers(events) == ["paused · resumed 1h later"]


# AC6(c)
def test_attached_hold_prompt_reply_gets_no_marker() -> None:
    events = [_quest(),
              ev("1", "step_started", **AGENT, timestamp=_at(0)),
              ev("2", "workflow_paused", **CTRL_C, timestamp=_at(0)),
              ev("3", "workflow_resumed", in_process=True, timestamp=_at(12))]
    assert _markers(events) == []


# AC7
def test_a_pause_between_steps_places_the_marker_at_phase_level() -> None:
    first = {**SCRIPT, "step": "s1", "label": "one"}
    second = {**SCRIPT, "step": "s2", "label": "two"}
    events = [_quest(),
              ev("1", "step_started", **first, timestamp=_at(0)),
              ev("2", "step_completed", step="s1", kind="script", **_DONE, timestamp=_at(5)),
              ev("3", "workflow_paused", **CTRL_C, timestamp=_at(10)),
              ev("4", "workflow_resumed", timestamp=_at(130)),
              ev("5", "step_started", **second, timestamp=_at(130)),
              ev("6", "step_completed", step="s2", kind="script", **_DONE, timestamp=_at(135))]
    assert [type(c) for c in _phase(events).children] == [
        nv.StepNode, nv.ResumeMarkerNode, nv.StepNode]
    lines = _log(events)
    assert (lines.index("one · complete · 5s") < lines.index(MARKER_2M)
            < lines.index("two · complete · 5s"))


# AC8
@pytest.mark.parametrize("events", [
    _agent_run({"reason": "reauth_needed"}),
    [_quest(),
     ev("1", "step_started", **SCRIPT, timestamp=_at(0)),
     ev("2", "workflow_paused", reason="reauth_needed", timestamp=_at(10)),
     ev("3", "workflow_resumed", timestamp=_at(130)),
     ev("4", "step_started", **SCRIPT, timestamp=_at(130)),
     ev("5", "step_completed", step="verify", kind="script", **_DONE, timestamp=_at(140))],
], ids=["agent", "visible-script"])
def test_a_self_stopped_run_keeps_its_pause_line_then_the_marker(events: Events) -> None:
    lines = _log(events)
    pause_at = next(i for i, ln in enumerate(lines) if ln.startswith("paused — "))
    assert pause_at < lines.index(MARKER_2M)


def test_two_cycles_on_one_agent_step_show_two_markers_in_order() -> None:
    cycle = [ev("10", "workflow_paused", **CTRL_D, timestamp=_at(1000)),
             ev("11", "workflow_resumed", timestamp=_at(1045)),
             ev("12", "step_started", **AGENT, resumed=True, restarted=False,
                timestamp=_at(1045))]
    events = _agent_run()
    assert _markers([*events[:-1], *cycle, events[-1]]) == [
        MARKER_2M, "paused · resumed 45s later"]


# AC10(a)
def test_live_equals_batch_across_a_continuation_resume() -> None:
    events = _agent_run()
    assert MARKER_2M in _live(events)
    assert _live(events) == _log(events)


# AC13
def test_marker_uses_the_lifecycle_color_and_no_glyph() -> None:
    node = nv.ResumeMarkerNode(gap_s=120)
    colored = list(rt._render(node, depth=0, color=True, width=80))
    assert colored == [pal.ansi(MARKER_2M, *pal.NODE_COLORS["lifecycle"], enabled=True)]
    plain = list(rt._render(node, depth=0, color=False, width=80))
    assert plain == [MARKER_2M]


# AC16
def test_structural_repr_and_incremental_parity() -> None:
    assert nv.structural_repr(nv.ResumeMarkerNode(gap_s=120)) == {
        "t": "ResumeMarkerNode", "gap_s": 120}
    events = _agent_run()
    builder = nv.NarrativeBuilder()
    for e in events:
        builder.add(e)
    assert (nv.structural_repr(builder.tree.root)
            == nv.structural_repr(nv.build_tree(events).root))


# AC18(a)
def test_visible_script_step_keeps_its_collapse() -> None:
    assert [ln for ln in _log(_script_run()) if ln and "phase" not in ln] == [
        MARKER_2M45, COLLAPSED]


# AC18(b)
def test_hidden_script_step_shows_only_the_marker() -> None:
    assert [ln for ln in _log(_script_run(show_label=False)) if ln and "phase" not in ln] == [
        MARKER_2M45]


# AC18(c)
def test_show_output_script_step_holds_the_marker_between_its_output() -> None:
    lines = _log(_script_run(show_output=True, before=("out before",), after=("out after",)))
    assert lines.index("• out before") < lines.index(MARKER_2M45) < lines.index("• out after")


# AC18(d)
def test_visible_script_step_with_two_cycles_keeps_markers_then_collapse() -> None:
    assert [ln for ln in _log(_two_cycle_script()) if ln and "phase" not in ln] == [
        MARKER_2M45, "paused · resumed 1m later", COLLAPSED]


# AC19
@pytest.mark.parametrize("events", [
    _script_run(),
    _script_run(show_label=False),
    _script_run(show_output=True, before=("out before",), after=("out after",)),
    _two_cycle_script(),
    _nested_script(),
], ids=["visible", "hidden", "show-output", "two-cycles", "nested"])
def test_live_equals_batch_for_held_steps(events: Events) -> None:
    assert MARKER_2M45 in _live(events)
    assert _live(events) == _log(events)


# Held step whose parent is another step, not the phase.
def test_held_step_marker_goes_ahead_of_it_in_its_parent_step() -> None:
    events = _nested_script()
    tree_events = events[:events.index(next(e for e in events if e["id"] == "5"))]
    outer = _phase(tree_events).children[0]
    assert isinstance(outer, nv.StepNode) and outer.name == "A"
    assert [type(c) for c in outer.children] == [nv.ResumeMarkerNode, nv.StepNode]
    assert outer.children[1].name == "B"
    lines = _log(events)
    assert lines.index("step A") < lines.index(MARKER_2M45) < lines.index("step B")


def _replayed(events: Events) -> str:
    # The daemon writes workflow_resumed to disk before the client's replay reads it.
    cut = max(i for i, e in enumerate(events) if e["type"] == "workflow_resumed") + 1
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    for e in events[:cut]:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})
    for e in events[cut:]:
        drv.on_event(e)
    drv.finish()
    return out.getvalue()


# AC9(a), (b)
def test_restart_keeps_the_marker_and_wipes_the_partial_transcript() -> None:
    lines = [ln for ln in _log(_agent_run(restarted=True)) if ln]
    assert "• before" not in lines
    header = next(i for i, ln in enumerate(lines) if "(resume)" in ln)
    assert header < lines.index(MARKER_2M) < lines.index("• after")


# AC9(c)
def test_restart_accumulates_markers_in_order() -> None:
    cycle = [ev("10", "workflow_paused", **CTRL_D, timestamp=_at(1000)),
             ev("11", "workflow_resumed", timestamp=_at(1045)),
             ev("12", "step_started", **AGENT, resumed=True, restarted=True,
                timestamp=_at(1045))]
    events = _agent_run(restarted=True)
    assert _markers([*events[:-1], *cycle, events[-1]]) == [
        MARKER_2M, "paused · resumed 45s later"]


# AC10(b): pins today's restart output, which diverges from batch by design.
def test_live_restart_prints_one_marker_and_is_otherwise_unchanged() -> None:
    live = _live(_agent_run(restarted=True, gap=120))
    baseline = _live(_agent_run(restarted=True, gap=0.5))
    assert live.count(MARKER_2M) == 1
    assert [ln for ln in live if ln and ln != MARKER_2M] == [ln for ln in baseline if ln]


# AC11
@pytest.mark.parametrize("make", [
    _script_run,
    lambda **kw: _script_run(show_label=False, **kw),
], ids=["visible-script", "hidden-script"])
def test_replay_suppressed_driver_prints_no_marker_and_an_unchanged_body(make: Any) -> None:
    shown = _replayed(make())
    assert MARKER not in shown
    assert shown == _replayed(make(resumed={"in_process": True}))


# Resume tail AC4(a): a marker after the step's last prose is part of the tail, as in imp log.
def test_replay_suppressed_driver_shows_a_marker_inside_the_resume_tail() -> None:
    shown = _replayed(_agent_run()).splitlines()
    log = _log(_agent_run())
    assert MARKER_2M in shown
    assert shown[0] == ""                   # the bare-header blank
    assert shown[1:] == log[log.index("• before"):]


# Resume tail AC4(b): a marker before the tail start stays hidden.
def test_replay_suppressed_driver_hides_a_marker_before_the_tail_start() -> None:
    events = _agent_run()
    cut = next(i for i, e in enumerate(events) if e["id"] == "7")   # attach before the close
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True, preceded_by_blank=True)
    for e in events[:cut]:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})
    assert out.getvalue() == "• after\n"


# AC11(b): the two script shapes above replay to nothing, so pin one with a body.
def test_replay_suppressed_driver_keeps_a_script_steps_later_output() -> None:
    shown = _replayed(_script_run(show_output=True, after=("out after",)))
    assert "out after" in shown
    assert MARKER not in shown
    assert shown == _replayed(_script_run(show_output=True, after=("out after",),
                                          resumed={"in_process": True}))


# AC12(a), (b)
def test_markdown_and_html_carry_the_marker() -> None:
    md = rm.render_markdown(nv.build_tree(_agent_run()), full=False)
    assert f"**{MARKER_2M}**" in md.splitlines()
    assert f"<strong>{MARKER_2M}</strong>" in render_html(md, title="t")


# AC12(c)
@pytest.mark.parametrize("start", [{}, {"show_label": False}], ids=["visible", "hidden"])
def test_markdown_keeps_a_held_script_steps_marker(start: dict[str, Any]) -> None:
    events = _script_run(**start)
    md = rm.render_markdown(nv.build_tree(events), full=False)
    assert f"**{MARKER_2M45}**" in md.splitlines()
    assert MARKER_2M45 in _log(events)
