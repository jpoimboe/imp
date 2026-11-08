"""Resume tail: the replayed top step's last agent message and everything after it."""
from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import pytest

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree
from tests._event_fixtures import agent_text, ev
from tests.test_interrupt_recap_resume import BLOCK, PROSE, _client_output, _replayed
from tests.test_narrative_interrupt_recap import (
    LEAD,
    SPEC_REVIEW,
    Event,
    _dispatch,
    _sub_work,
    _task_lead,
)

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


# AC3(a)
def test_an_open_burst_at_the_tail_end_renders_once_with_its_final_count() -> None:
    replayed = [ev("q", "quest_created", quest_id=1, title="t"), _task_lead(),
                agent_text("p1", "Running the tests now.", agent_id=LEAD),
                *_tool("1", "t1", "make check")]
    live = [*_tool("2", "t2", "ls"), agent_text("p2", "Tests pass.", agent_id=LEAD)]
    drv, out = _attach(replayed)
    assert out.getvalue() == "• Running the tests now.\n"
    for e in live:
        drv.on_event(e)
    drv.finish()
    rows = out.getvalue().splitlines()
    assert [r for r in rows if "bash" in r] == ["  2 calls · bash"]
    assert rows == _log_from([*replayed, *live], "• Running the tests now.")


# AC3(b)
def test_a_running_subagent_at_the_tail_end_renders_when_it_finishes() -> None:
    replayed = [ev("q", "quest_created", quest_id=1, title="t"), _task_lead(),
                agent_text("p1", "Dispatching it.", agent_id=LEAD),
                _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1")]
    done = ev("r-d1", "tool_result", LEAD, id="blk-d1", content="done", id_ns="block")
    drv, out = _attach(replayed)
    assert out.getvalue() == "• Dispatching it.\n"
    drv.on_event(done)
    drv.finish()
    rows = out.getvalue().splitlines()
    assert len([r for r in rows if r.startswith("  coder")]) == 1
    assert rows == _log_from([*replayed, done], "• Dispatching it.")


_PROVIDER_ERROR = "API Error: 529 overloaded."


# AC3(c)
def test_error_pending_tail_prose_renders_when_its_turn_resolves() -> None:
    tagged = ev("e1", "agent_message", LEAD, provider="anthropic", model="m", usage={},
                stop_reason=None, provider_error=True,
                raw={"content": [{"type": "text", "text": _PROVIDER_ERROR}]})
    replayed = [ev("q", "quest_created", quest_id=1, title="t"), _task_lead(),
                agent_text("p0", "Earlier prose.", agent_id=LEAD), tagged]
    live = [agent_text("p1", "Retrying now.", agent_id=LEAD)]
    drv, out = _attach(replayed)
    assert out.getvalue() == ""
    for e in live:
        drv.on_event(e)
    drv.finish()
    rows = out.getvalue().splitlines()
    assert rows[0] == f"• {_PROVIDER_ERROR}"
    assert "Earlier prose." not in out.getvalue()
    assert rows == _log_from([*replayed, *live], f"• {_PROVIDER_ERROR}")


def _at(secs: int) -> str:
    return f"2026-01-01T00:00:{secs:02d}+00:00"


# Once a tail lifted suppression, a live step/phase close renders its breadcrumbs, as imp log does.
def test_a_live_close_after_the_tail_renders_the_step_and_phase_breadcrumbs() -> None:
    replayed = [ev("q", "quest_created", quest_id=1, title="t", timestamp=_at(0)),
                ev("s1", "step_started", step="spec-review", kind="agent", phase="spec",
                   label="reviewing spec", timestamp=_at(1)),
                {**agent_text("p1", "Spec looks good.", agent_id=SPEC_REVIEW),
                 "timestamp": _at(2)}]
    live = [ev("c1", "step_completed", step="spec-review", kind="agent",
               action={"verb": "continue"}, timestamp=_at(9)),
            ev("s2", "step_started", step="draft", kind="branch", phase="plan",
               timestamp=_at(10)),
            ev("c2", "step_completed", step="draft", kind="branch",
               action={"verb": "continue"}, timestamp=_at(12))]
    drv, out = _attach(replayed, live)
    drv.finish()
    rows = out.getvalue().splitlines()
    assert "spec phase complete · 8s" in rows
    assert rows == _log_from([*replayed, *live], "• Spec looks good.")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# AC5(b): through the real SessionClient → run_terminal_client path.
@pytest.mark.anyio
async def test_attaching_to_a_running_session_renders_the_tail(tmp_path: Path) -> None:
    question = ev("q2", "awaiting_user_input", LEAD, steer_hint="",
                  last_agent_message="Which file?", interrupt=False)
    frames = [*_replayed([_task_lead(), agent_text("p1", PROSE, agent_id=LEAD)]),
              BOUNDARY, question]
    assert await _client_output(tmp_path, frames) == BLOCK + "\n"
