"""The trailing `paused · <label>` line `imp log` ends on after an unresumed manual stop."""
from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_markdown as rm
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_html import render_html
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev

Events = list[dict[str, Any]]

LABELS = [
    ("pause", "paused · /pause"),
    ("ctrl-d", "paused · Ctrl-D"),
    ("ctrl-c", "paused · Ctrl-C"),
    ("sigterm", "paused · SIGTERM"),
    ("sighup", "paused · SIGHUP"),
    ("sigquit", "paused · SIGQUIT"),
]
BUILD = {"step": "build", "kind": "agent", "phase": "implement", "model": "m",
         "provider": "anthropic"}
_PAUSED_STEP: dict[str, Any] = {"action": {"verb": "pause"}, "var_mutations": {}}
_DONE: dict[str, Any] = {"action": {"verb": "continue"}, "var_mutations": {}}


def _at(secs: float) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=secs)).isoformat()


def _manual(token: str) -> dict[str, Any]:
    return {"reason": "interrupt", "interrupt_reason": token}


def _stopped(pause: dict[str, Any], *, warn: bool = False, then: Events | None = None) -> Events:
    return [ev("0", "quest_created", quest_id=1, title="x", timestamp=_at(0)),
            ev("1", "step_started", **BUILD, timestamp=_at(0)),
            *([ev("2", "runner_warning", message="dropped block")] if warn else []),
            ev("3", "step_completed", step="build", kind="agent", **_PAUSED_STEP,
               timestamp=_at(5)),
            ev("4", "workflow_paused", **pause, timestamp=_at(10)),
            *(then or [])]


def _resumed_run() -> Events:
    return [ev("5", "workflow_resumed", timestamp=_at(130)),
            ev("6", "step_started", **BUILD, resumed=True, timestamp=_at(130)),
            ev("7", "step_completed", step="build", kind="agent", **_DONE,
               timestamp=_at(140))]


def _lines(events: Events, *, color: bool = False) -> list[str]:
    return list(render_tree(nv.build_tree(events), color=color, width=80))


def _leave_markers(events: Events) -> list[Any]:
    return [c for c in nv.build_tree(events).root.children
            if isinstance(c, nv.LeaveMarkerNode)]


@pytest.mark.parametrize(("token", "line"), LABELS)
def test_unresumed_manual_stop_ends_on_its_label_after_a_blank(token: str, line: str) -> None:
    lines = _lines(_stopped(_manual(token)))
    assert lines[-2:] == ["", line]
    assert f"paused ({token})" not in lines


@pytest.mark.parametrize(("token", "line"), LABELS)
def test_trailing_line_follows_the_warnings_digest(token: str, line: str) -> None:
    lines = _lines(_stopped(_manual(token), warn=True))
    assert lines[-5:] == ["", "1 warning this run", "  • dropped block", "", line]


def test_a_resumed_stop_keeps_only_the_resume_marker() -> None:
    lines = _lines(_stopped(_manual("pause"), then=_resumed_run()))
    assert [ln for ln in lines if ln.startswith("paused · ")] == ["paused · resumed 2m later"]


def test_the_last_pause_decides_the_label() -> None:
    events = _stopped(_manual("pause"), then=[
        *_resumed_run(),
        ev("8", "workflow_paused", **_manual("sigterm"), timestamp=_at(150))])
    lines = _lines(events)
    assert lines[-2:] == ["", "paused · SIGTERM"]
    assert [ln for ln in lines if ln.startswith("paused · ")] == [
        "paused · resumed 2m later", "paused · SIGTERM"]


def test_a_later_awaiting_user_pause_gets_no_trailing_line() -> None:
    events = _stopped(_manual("pause"), then=[
        *_resumed_run(),
        ev("8", "workflow_paused", reason="awaiting_user", timestamp=_at(150))])
    assert _leave_markers(events) == []


@pytest.mark.parametrize("pause", [
    {"reason": "awaiting_user"},
    {"reason": "max_loops"},
    {"reason": "quota"},
    {"reason": "interrupt", "interrupt_reason": "detached"},
    {"reason": "interrupt"},
])
def test_a_non_manual_pause_gets_no_trailing_line(pause: dict[str, Any]) -> None:
    events = _stopped(pause)
    assert _leave_markers(events) == []
    assert not any(ln.startswith("paused · ") for ln in _lines(events))


def test_max_loops_keeps_its_pause_line() -> None:
    lines = _lines(_stopped({"reason": "max_loops"}))
    assert any(ln.startswith("paused — a step kept sending work back") for ln in lines)


@pytest.mark.parametrize("terminal", [
    ev("9", "workflow_completed", commits=[]),
    ev("9", "quest_merged", base="main", commit="37dde900abc"),
])
def test_a_completed_or_merged_run_gets_no_trailing_line(terminal: dict[str, Any]) -> None:
    events = _stopped(_manual("pause"), then=[terminal])
    assert _leave_markers(events) == []
    assert "paused · /pause" not in _lines(events)


def test_trailing_line_uses_the_lifecycle_color_and_no_glyph() -> None:
    last = _lines(_stopped(_manual("pause")), color=True)[-1]
    assert last == pal.ansi("paused · /pause", *pal.NODE_COLORS["lifecycle"], enabled=True)
    assert "⏸" not in last


def test_markdown_and_html_carry_the_trailing_line() -> None:
    md = rm.render_markdown(nv.build_tree(_stopped(_manual("pause"))), full=False)
    assert "**paused · /pause**" in md
    assert "<strong>paused · /pause</strong>" in render_html(md, title="t")


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_the_live_view_never_prints_the_trailing_line() -> None:
    events = _stopped(_manual("pause"), warn=True)
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert "1 warning this run" in out.getvalue()
    assert "paused · /pause" not in out.getvalue()
    assert _lines(events)[-1] == "paused · /pause"


def test_structural_repr_carries_the_reason() -> None:
    assert nv.structural_repr(nv.LeaveMarkerNode("pause", _at(10))) == {
        "t": "LeaveMarkerNode", "reason": "pause"}


def _window_markers(window: Events, full: Events) -> list[Any]:
    from imp.cli.main import _log_tree
    return [c for c in _log_tree(window, full).root.children
            if isinstance(c, nv.LeaveMarkerNode)]


def test_a_window_ending_on_a_later_resumed_stop_gets_no_trailing_line() -> None:
    window = _stopped(_manual("ctrl-c"))
    assert _window_markers(window, [*window, *_resumed_run()]) == []


def test_a_window_ending_on_an_earlier_same_reason_stop_gets_no_trailing_line() -> None:
    window = _stopped(_manual("ctrl-c"))
    full = [*window, *_resumed_run(),
            ev("8", "workflow_paused", **_manual("ctrl-c"), timestamp=_at(150))]
    assert _window_markers(window, full) == []


def test_a_window_ending_on_the_final_stop_keeps_its_trailing_line() -> None:
    full = _stopped(_manual("ctrl-c"))
    assert _window_markers(full[1:], full) == [nv.LeaveMarkerNode("ctrl-c", _at(10))]


def test_incremental_tree_omits_the_batch_only_node() -> None:
    events = _stopped(_manual("ctrl-d"), warn=True)
    b = nv.NarrativeBuilder()
    for e in events:
        b.add(e)
    b.finalize()
    batch = nv.structural_repr(nv.build_tree(events).root)
    assert batch["children"][-1] == {"t": "LeaveMarkerNode", "reason": "ctrl-d"}
    del batch["children"][-1]
    assert batch == nv.structural_repr(b.tree.root)
