"""Resume tail: the replayed top step's last agent message and everything after it."""
from __future__ import annotations

import io
from collections.abc import Sequence

import pytest

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree
from tests._event_fixtures import agent_text, ev
from tests.test_narrative_interrupt_recap import LEAD, Event, _dispatch, _task_lead

BOUNDARY: Event = {"type": "replay_complete"}


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _attach(replayed: list[Event], live: Sequence[Event] = ()) -> tuple[LiveDriver, _Pipe]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True, preceded_by_blank=True)
    for e in replayed:
        drv.on_event({**e, "_replay": True})
    drv.on_event(BOUNDARY)
    for e in live:
        drv.on_event(e)
    return drv, out


def _log_from(events: list[Event], first_row: str) -> list[str]:
    rows = list(render_tree(nv.build_tree(events), color=False, width=80))
    return rows[rows.index(first_row):]


def _tool(eid: str, call_id: str, command: str) -> list[Event]:
    return [ev(f"c-{eid}", "tool_call", LEAD, id=call_id, name="bash",
               input={"command": command}),
            ev(f"r-{eid}", "tool_result", LEAD, id=call_id, content="ok")]


_LEFT_MID_TOOL = [
    ev("q", "quest_created", quest_id=1, title="t"),
    _task_lead(),
    agent_text("p0", "Opening turn.", agent_id=LEAD),
    agent_text("p1", "Which file?", agent_id=LEAD),
    ev("g1", "awaiting_user_input", LEAD),
    ev("u1", "user_message", LEAD, role="user",
       content=[{"type": "text", "text": "the parser"}]),
    *_tool("1", "t1", "make check"),
    ev("wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
    ev("wr", "workflow_resumed"),
]
_CONTINUED = [
    _task_lead("s2", resumed=True, restarted=False),
    agent_text("p2", "Back at it.", agent_id=LEAD),
    ev("sc", "step_completed", step="task-lead", kind="agent", action={"verb": "continue"}),
]


# AC9
def test_tail_rows_match_imp_log_on_a_continuation_resume() -> None:
    drv, out = _attach(_LEFT_MID_TOOL)
    at_boundary = out.getvalue()
    assert at_boundary.startswith("• Which file?\n")
    assert "> the parser" in at_boundary
    assert "Opening turn." not in at_boundary
    for e in _CONTINUED:
        drv.on_event(e)
    drv.finish()
    assert out.getvalue().splitlines() == _log_from(
        [*_LEFT_MID_TOOL, *_CONTINUED], "• Which file?")


# AC8
@pytest.mark.parametrize("replayed", [
    pytest.param([], id="no-step"),
    pytest.param([_task_lead(), ev("t1", "thinking", LEAD, content="hmm"),
                  *_tool("1", "t1", "make check")], id="no-prose"),
    pytest.param([_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
                  agent_text("sp", "Coder prose.", agent_id="sub:d1"),
                  ev("r-d1", "tool_result", LEAD, id="blk-d1", content="done",
                     id_ns="block")], id="subagent-prose-only"),
    pytest.param([ev("a", "step_started", step="spec", kind="agent", phase="spec",
                     label="writing spec"),
                  agent_text("ap", "Spec drafted.", agent_id="step:spec/spec"),
                  ev("ac", "step_completed", step="spec", kind="agent",
                     action={"verb": "continue"}),
                  _task_lead(), ev("t1", "thinking", LEAD, content="hmm")],
                 id="prose-only-in-an-earlier-step"),
])
def test_no_tail_without_top_level_prose_in_the_top_step(replayed: list[Event]) -> None:
    _drv, out = _attach(replayed)
    assert out.getvalue() == ""


# AC7(b): the inert path printed the question itself, then echoed the reply.
def test_an_echoed_reply_suppresses_the_tail() -> None:
    question = "Which file should I edit?"
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    drv.echo_submitted_reply("go ahead")
    for e in [ev("q", "quest_created", quest_id=1, title="t"), _task_lead(),
              agent_text("p1", question, agent_id=LEAD),
              ev("g", "awaiting_user_input", LEAD),
              ev("wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-d"),
              ev("wr", "workflow_resumed")]:
        drv.on_event({**e, "_replay": True})
    drv.on_event(BOUNDARY)
    drv.on_event(ev("u", "user_message", LEAD, role="user",
                    content=[{"type": "text", "text": "go ahead"}]))
    drv.on_event(agent_text("p2", "On it.", agent_id=LEAD))
    shown = out.getvalue()
    assert question not in shown
    assert shown.count("> go ahead") == 1
    assert "• On it." in shown
