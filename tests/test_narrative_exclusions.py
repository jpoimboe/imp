"""Paused and human-wait time excluded from scrollback durations."""
from __future__ import annotations

import io
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli import render_markdown as rm
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_summary import build_summary
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev


def _ts(secs: int) -> str:
    return f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00"


def test_tracker_closes_spans_incrementally_as_events_arrive() -> None:
    tracker = nv.ExclusionTracker()
    tracker.feed(ev("1", "step_started", step="verify", phase="implement", timestamp=_ts(0)))
    tracker.feed(ev("2", "workflow_paused", reason="ctrl-c", timestamp=_ts(10)))
    assert tracker.exclusions.spans == ()        # still open: nothing excluded yet
    tracker.feed(ev("3", "step_started", step="verify", phase="implement", timestamp=_ts(175)))
    assert tracker.exclusions.spans == ((_ts(10), _ts(175)),)
    assert tracker.exclusions.overlap(_ts(0), _ts(195)) == 165.0


_DONE = {"action": {"verb": "continue"}, "var_mutations": {}}


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _log_lines(events: list[dict[str, Any]]) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def _live_text(events: list[dict[str, Any]]) -> str:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue()


def _breadcrumb_seconds(events: list[dict[str, Any]]) -> dict[str, float | None]:
    tree = nv.build_tree(events)
    return {step.name: crumb.elapsed_s
            for phase in tree.root.children if isinstance(phase, nv.PhaseNode)
            for step in phase.children if isinstance(step, nv.StepNode)
            for crumb in step.children if isinstance(crumb, nv.BreadcrumbLine)}


def _paused_verify(*, resumed: bool) -> list[dict[str, Any]]:
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", timestamp=_ts(0)),
        ev("3", "workflow_paused", reason="ctrl-c", timestamp=_ts(10)),
    ]
    if resumed:
        events.append(ev("4", "workflow_resumed", timestamp=_ts(175)))
    events += [
        ev("5", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", timestamp=_ts(175)),
        ev("6", "step_completed", step="verify", kind="script", **_DONE, timestamp=_ts(195)),
    ]
    return events


# AC1 (resumed) / AC3 (attached client: no workflow_resumed, the re-emitted
# step_started closes the pause).
@pytest.mark.parametrize("resumed", [True, False], ids=["resumed", "attached"])
def test_paused_script_step_collapses_to_its_work_time(resumed: bool) -> None:
    events = _paused_verify(resumed=resumed)
    hits = [ln for ln in _log_lines(events) if "running tests" in ln]
    assert hits == ["running tests · complete · 30s"]   # 195s wall - 165s paused, not 3m 15s
    assert "running tests · complete · 30s" in _live_text(events)
    md = rm.render_markdown(nv.build_tree(events), full=False)
    assert "_running tests · complete · 30s_" in md


def test_continued_agent_step_excludes_the_pause() -> None:
    # AC2: resumed=True, restarted=False keeps the original start_ts.
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", timestamp=_ts(0)),
        ev("3", "thinking", agent_id="step:review/review", content="hmm", timestamp=_ts(2)),
        ev("4", "workflow_paused", reason="ctrl-c", timestamp=_ts(10)),
        ev("5", "workflow_resumed", timestamp=_ts(100)),
        ev("6", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", resumed=True, restarted=False, timestamp=_ts(100)),
        ev("7", "thinking", agent_id="step:review/review", content="back", timestamp=_ts(105)),
        ev("8", "step_completed", step="review", kind="agent", **_DONE, timestamp=_ts(120)),
    ]
    assert _breadcrumb_seconds(events) == {"review": 30.0}   # 120s wall - 90s paused
    body = "\n".join(_log_lines(events))
    assert "reviewing · complete · 30s" in body
    assert "2m 0s" not in body


def test_restarted_agent_step_measures_from_the_restart() -> None:
    # restarted=True restamps start_ts, so the pause is outside the window.
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", timestamp=_ts(0)),
        ev("3", "thinking", agent_id="step:review/review", content="hmm", timestamp=_ts(2)),
        ev("4", "workflow_paused", reason="ctrl-c", timestamp=_ts(10)),
        ev("5", "workflow_resumed", timestamp=_ts(100)),
        ev("6", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", resumed=True, restarted=True, timestamp=_ts(100)),
        ev("7", "thinking", agent_id="step:review/review", content="back", timestamp=_ts(105)),
        ev("8", "step_completed", step="review", kind="agent", **_DONE, timestamp=_ts(130)),
    ]
    assert _breadcrumb_seconds(events) == {"review": 30.0}   # restamped at 100s; 130s - 100s
    assert "reviewing · complete · 30s" in "\n".join(_log_lines(events))


def test_human_wait_inside_a_step_is_excluded() -> None:
    # AC4
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           label="writing spec", timestamp=_ts(0)),
        ev("3", "awaiting_user_input", timestamp=_ts(10)),
        ev("4", "user_message", role="user", subtype="reply",
           content=[{"type": "text", "text": "yes"}], timestamp=_ts(40)),
        ev("5", "step_completed", step="spec-writer", kind="agent", **_DONE, timestamp=_ts(60)),
    ]
    assert _breadcrumb_seconds(events) == {"spec-writer": 30.0}   # 60s wall - 30s wait


@pytest.mark.parametrize("opener", ["workflow_paused", "awaiting_user_input"])
def test_span_still_open_at_completion_is_excluded(opener: str) -> None:
    # AC8: the step's own step_completed closes the span.
    extra = {"reason": "ctrl-c"} if opener == "workflow_paused" else {}
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", timestamp=_ts(0)),
        ev("3", opener, **extra, timestamp=_ts(10)),
        ev("4", "step_completed", step="verify", kind="script", **_DONE, timestamp=_ts(40)),
    ]
    assert _breadcrumb_seconds(events) == {"verify": 10.0}   # 40s wall - 30s open span


def test_backoff_and_pause_are_both_subtracted() -> None:
    # AC6(a): retry backoff subtraction is unchanged and stacks with the pause.
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", timestamp=_ts(0)),
        ev("3", "thinking", agent_id="step:review/review", content="hmm", timestamp=_ts(1)),
        ev("4", "agent_retry", category="auth", attempt=1, backoff_seconds=30.0,
           timestamp=_ts(2)),
        ev("5", "thinking", agent_id="step:review/review", content="back", timestamp=_ts(32)),
        ev("6", "workflow_paused", reason="ctrl-c", timestamp=_ts(40)),
        ev("7", "workflow_resumed", timestamp=_ts(100)),
        ev("8", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", resumed=True, restarted=False, timestamp=_ts(100)),
        ev("9", "step_completed", step="review", kind="agent", **_DONE, timestamp=_ts(110)),
    ]
    assert _breadcrumb_seconds(events) == {"review": 20.0}   # 110s - 30s backoff - 60s paused


def test_pause_during_backoff_is_not_double_subtracted() -> None:
    # A pause wakes the backoff sleep, so backoff_ended precedes workflow_paused.
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", timestamp=_ts(0)),
        ev("3", "thinking", agent_id="step:review/review", content="hmm", timestamp=_ts(1)),
        ev("4", "agent_retry", category="auth", attempt=1, backoff_seconds=300.0,
           timestamp=_ts(2)),
        ev("5", "backoff_ended", reason="elapsed", timestamp=_ts(20)),
        ev("6", "workflow_paused", reason="ctrl-c", timestamp=_ts(20)),
        ev("7", "workflow_resumed", timestamp=_ts(100)),
        ev("8", "step_started", step="review", kind="agent", phase="review",
           label="reviewing", model="m", resumed=True, restarted=False, timestamp=_ts(100)),
        ev("9", "step_completed", step="review", kind="agent", **_DONE, timestamp=_ts(110)),
    ]
    assert _breadcrumb_seconds(events) == {"review": 12.0}   # 110s - 18s backoff - 80s paused
    assert "reviewing · complete · 12s" in "\n".join(_log_lines(events))


def test_step_durations_match_ledger_work_time() -> None:
    # AC7: no backoff, no contended lane, no restarted resume.
    events = [
        ev("1", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           label="writing spec", timestamp=_ts(0)),
        ev("3", "awaiting_user_input", timestamp=_ts(10)),
        ev("4", "user_message", role="user", subtype="reply",
           content=[{"type": "text", "text": "yes"}], timestamp=_ts(40)),
        ev("5", "step_completed", step="spec-writer", kind="agent", **_DONE, timestamp=_ts(60)),
        ev("6", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", timestamp=_ts(60)),
        ev("7", "workflow_paused", reason="ctrl-c", timestamp=_ts(70)),
        ev("8", "workflow_resumed", timestamp=_ts(200)),
        ev("9", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", timestamp=_ts(200)),
        ev("10", "step_completed", step="verify", kind="script", **_DONE, timestamp=_ts(215)),
    ]
    ledger = {row.step: row.work_seconds for row in build_summary(events)}
    assert ledger == {"spec-writer": 30.0, "verify": 25.0}
    assert _breadcrumb_seconds(events) == pytest.approx(ledger)


def _phase_seconds(events: list[dict[str, Any]]) -> dict[str, float | None]:
    tree = nv.build_tree(events)
    return {phase.name: nv.phase_elapsed(phase)
            for phase in tree.root.children if isinstance(phase, nv.PhaseNode)}


def _step(eid: int, name: str, phase: str, start: int, end: int) -> list[dict[str, Any]]:
    return [
        ev(f"{eid}s", "step_started", step=name, kind="script", phase=phase,
           label=name, timestamp=_ts(start)),
        ev(f"{eid}c", "step_completed", step=name, kind="script", **_DONE, timestamp=_ts(end)),
    ]


def test_phase_excludes_a_pause_inside_one_of_its_steps() -> None:
    # AC5(a)
    events = [ev("0", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
              *_paused_verify(resumed=True)[1:]]
    assert _phase_seconds(events) == {"implement": 30.0}   # 195s wall - 165s paused


@pytest.mark.parametrize("resumed", [True, False], ids=["resumed", "attached"])
def test_phase_excludes_a_pause_between_two_of_its_steps(resumed: bool) -> None:
    # AC5(b): paused after s1 completed; s2's step_started closes it if nothing else did.
    events = [ev("0", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
              *_step(1, "s1", "implement", 0, 10),
              ev("p", "workflow_paused", reason="ctrl-c", timestamp=_ts(15))]
    if resumed:
        events.append(ev("r", "workflow_resumed", timestamp=_ts(115)))
    events += _step(2, "s2", "implement", 115, 125)
    assert _phase_seconds(events) == {"implement": 25.0}   # 125s wall - 100s paused


def test_pause_across_a_phase_boundary_is_charged_to_neither_phase() -> None:
    # AC5(c): opens after spec's last step, closed by implement's first step; stays on spec.
    events = [ev("0", "quest_created", quest_id=1, title="x", timestamp=_ts(0)),
              *_step(1, "s1", "spec", 0, 10),
              ev("p", "workflow_paused", reason="ctrl-c", timestamp=_ts(12)),
              *_step(2, "s2", "implement", 100, 130)]
    assert _phase_seconds(events) == {"spec": 10.0, "implement": 30.0}
    body = "\n".join(_log_lines(events))
    assert "spec phase complete · 10s" in body
    spans = {p.name: p.exclusion_spans
             for p in nv.build_tree(events).root.children if isinstance(p, nv.PhaseNode)}
    assert spans == {"spec": [(_ts(12), _ts(100))], "implement": []}
