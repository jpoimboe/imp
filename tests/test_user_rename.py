"""`imp edit --title` renames render as the user's action, at their log position."""
from __future__ import annotations

from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from tests._event_fixtures import agent_text, ev
from tests.test_narrative_resume_marker import (
    AGENT,
    AID,
    CTRL_D,
    MARKER_2M,
    MARKER_2M45,
    _agent_run,
    _at,
    _live,
    _log,
    _phase,
    _Pipe,
    _script_run,
)

Events = list[dict[str, Any]]
LINE = 'title set to "X" · by you'


def _user_rename(eid: str, new: str, *, old: str = "x", at: float = 60) -> dict[str, Any]:
    return ev(eid, "user_renamed", id=1, old_title=old, new_title=new, timestamp=_at(at))


def _during_pause(events: Events, *renames: dict[str, Any]) -> Events:
    i = next(i for i, e in enumerate(events) if e["type"] == "workflow_resumed")
    return [*events[:i], *renames, *events[i:]]


def _stripped(events: Events) -> list[str]:
    return [ln.strip() for ln in _log(events)]


_PAUSED_SHAPES = [_agent_run(CTRL_D), _script_run(), _script_run(show_label=False)]
_PAUSED_IDS = ["agent", "held-script", "hidden-script"]
_PAUSED_MARKERS = list(zip(_PAUSED_SHAPES, [MARKER_2M, MARKER_2M45, MARKER_2M45], strict=True))


@pytest.mark.parametrize(("events", "marker"), _PAUSED_MARKERS, ids=_PAUSED_IDS)
def test_user_rename_during_pause_renders_before_the_resume_marker(
        events: Events, marker: str) -> None:
    lines = _stripped(_during_pause(events, _user_rename("r", "X")))
    i = lines.index(LINE)
    assert lines[i + 1] == "" and lines[i + 2] == marker
    assert not any("Set title to" in ln for ln in lines)


def test_user_rename_line_has_one_blank_line_each_side() -> None:
    lines = _stripped(_during_pause(_agent_run(CTRL_D), _user_rename("r", "X")))
    i = lines.index(LINE)
    assert lines[i - 1] == "" and lines[i - 2] != ""
    assert lines[i + 1] == "" and lines[i + 2] != ""
    node = nv.UserRenameNode(title="X")
    assert rt.spaces_before(node) and rt.spaces_after(node, full=False)


def test_successive_user_renames_each_render_in_order() -> None:
    events = _during_pause(_agent_run(CTRL_D), _user_rename("r1", "First"),
                           _user_rename("r2", "Second", old="First", at=70))
    lines = _stripped(events)
    assert (lines.index('title set to "First" · by you')
            < lines.index('title set to "Second" · by you'))
    assert nv.build_tree(events).root.title == "Second"


def test_user_rename_mid_tool_burst_does_not_land_inside_the_burst() -> None:
    events = [
        ev("0", "quest_created", quest_id=1, title="x"),
        ev("1", "step_started", **AGENT),
        ev("2", "tool_call", agent_id=AID, id="c1", name="read_file", input={"path": "a"}),
        ev("3", "tool_result", agent_id=AID, id="c1", content="ok", error=False),
        ev("4", "user_renamed", id=1, old_title="x", new_title="X"),
        ev("5", "tool_call", agent_id=AID, id="c2", name="read_file", input={"path": "b"}),
        ev("6", "tool_result", agent_id=AID, id="c2", content="ok", error=False),
    ]
    step = _phase(events).children[0]
    kinds = [type(c) for c in step.children
             if isinstance(c, (nv.ToolBurstFold, nv.UserRenameNode))]
    assert kinds == [nv.ToolBurstFold, nv.UserRenameNode, nv.ToolBurstFold]


def test_held_agent_rename_renders_before_a_following_user_rename() -> None:
    events = [
        ev("0", "quest_created", quest_id=1, title=""),
        ev("1", "step_started", **AGENT),
        ev("2", "quest_renamed", agent_id=AID, id=1, old_title="", new_title="A"),
        ev("3", "user_renamed", id=1, old_title="A", new_title="B"),
        agent_text("4", "prose", agent_id=AID),
    ]
    lines = _stripped(events)
    assert lines.index('• Set title to "A"') < lines.index('title set to "B" · by you')
    assert nv.build_tree(events).root.title == "B"


def test_user_rename_survives_a_restarted_step() -> None:
    lines = _stripped(_during_pause(_agent_run(CTRL_D, restarted=True),
                                    _user_rename("r", "X")))
    assert lines.index(LINE) < lines.index(MARKER_2M)


@pytest.mark.parametrize("events", _PAUSED_SHAPES, ids=_PAUSED_IDS)
def test_live_equals_batch_with_a_user_rename(events: Events) -> None:
    events = _during_pause(events, _user_rename("r", "X"))
    live = _live(events)
    assert any(ln.strip() == LINE for ln in live)
    assert live == _log(events)


def test_user_rename_line_uses_the_lifecycle_color_of_the_resume_marker() -> None:
    node = nv.UserRenameNode(title="X")
    colored = list(rt._render(node, depth=0, color=True, width=80))
    assert colored == [pal.ansi(LINE, *pal.NODE_COLORS["lifecycle"], enabled=True)]
    marker = list(rt._render(nv.ResumeMarkerNode(gap_s=120), depth=0, color=True, width=80))
    assert colored[0].split(LINE)[0] == marker[0].split(MARKER_2M)[0]
    assert list(rt._render(node, depth=0, color=False, width=80)) == [LINE]


def test_user_rename_title_control_bytes_are_caret_escaped() -> None:
    node = nv.UserRenameNode(title="my\x07name")
    assert list(rt._render(node, depth=0, color=False, width=80)) == [
        'title set to "my^Gname" · by you']


_PAUSED_WITH_RENAME = [
    ev("0", "quest_created", quest_id=1, title="x", timestamp=_at(0)),
    ev("1", "step_started", **AGENT, timestamp=_at(0)),
    agent_text("2", "before", agent_id=AID),
    ev("3", "workflow_paused", **CTRL_D, timestamp=_at(10)),
    _user_rename("4", "X"),
]
_GATE = ev("5", "awaiting_user_input", agent_id=AID)


def _resumed(events: Events, *, cut: int) -> str:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    for e in events[:cut]:
        drv.on_event({**e, "_replay": True})
    drv.on_event({"type": "replay_complete"})
    for e in events[cut:]:
        drv.on_event(e)
    drv.finish()
    return out.getvalue()


def test_replayed_user_rename_is_not_reprinted_on_resume() -> None:
    out = _resumed([*_PAUSED_WITH_RENAME, _GATE], cut=len(_PAUSED_WITH_RENAME))
    assert "title set to" not in out


def test_live_user_rename_after_the_boundary_prints_once() -> None:
    out = _resumed([*_PAUSED_WITH_RENAME, _GATE], cut=len(_PAUSED_WITH_RENAME) - 1)
    assert out.count(LINE) == 1


def test_markdown_export_renders_user_rename_bold_with_escaped_title() -> None:
    from imp.cli.render_markdown import render_markdown

    root = nv.WorkflowNode(title="d")
    root.children = [nv.UserRenameNode(title="X"), nv.UserRenameNode(title="a <b>")]
    out = render_markdown(nv.NarrativeTree(root=root), full=False)
    assert '**title set to "X" · by you**' in out
    assert '**title set to "a &lt;b&gt;" · by you**' in out


def test_structural_repr_carries_the_user_rename_title() -> None:
    assert nv.structural_repr(nv.UserRenameNode(title="X")) == {
        "t": "UserRenameNode", "title": "X"}
