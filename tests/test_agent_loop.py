# tests/test_agent_loop.py
from types import SimpleNamespace

import pytest

from imp.agents.agent_loop import run_agent_loop
from imp.agents.loop_guard import LoopGuard
from imp.agents.loop_items import (
    AwaitingToolResults,
    EndOfTurn,
    Error,
    Thinking,
    ToolRequested,
    Warning,
)
from imp.agents.runner import ToolResultBlock
from imp.engine.state import InMemoryEventLog


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TrippingGuard:
    """A guard whose observe() always returns the given error object."""
    def __init__(self, err):
        self._err = err
        self.observe_calls = 0

    def observe(self, record):
        self.observe_calls += 1
        return self._err


class FakeSession:
    def __init__(self, turns, *, subagent_depth=0, invocation=None, elog=None,
                 guard=None, handoff_before=None, handoff_after=("none", None)):
        self._turns = list(turns)
        self._i = 0
        self.subagent_depth = subagent_depth
        self.invocation = invocation
        self.elog = elog or InMemoryEventLog()
        self.aid = "step:p/s"
        self.guard = guard if guard is not None else LoopGuard(interactive=False)
        self.ceiling = None
        self.tool_calls_used = 0
        self.ctx = None
        self.tools = {}
        self.opened = False
        self.sent = []            # (text, human) tuples from send()
        self.sent_results = []    # lists passed to send_tool_results()
        self.interjects = 0
        self._last_end_text = ""
        self._handoff_before = handoff_before
        self._handoff_after = handoff_after

    async def open(self):
        self.opened = True

    def cancelled(self):
        return False

    async def events(self):
        turn = self._turns[self._i]
        self._i += 1
        for it in turn:
            if isinstance(it, EndOfTurn):
                self._last_end_text = it.last_text
            yield it

    def send_tool_results(self, results):
        self.sent_results.append(results)

    def send(self, text, *, human):
        self.sent.append((text, human))

    def deliver_turn_boundary_interject(self):
        self.interjects += 1

    def subagent_deliverable(self):
        return self._last_end_text

    def handoff_before_nudge(self, last_text):
        return self._handoff_before

    def handoff_after_tools(self):
        return self._handoff_after


@pytest.mark.anyio
async def test_no_complete_step_error():
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession([[EndOfTurn(last_text="I'm stuck")]], invocation=inv)
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert sess.opened is True
    assert evs[-1]["type"] == "error"
    assert evs[-1]["payload"]["category"] == "agent_output_missing"
    assert evs[-1]["payload"]["kind"] == "no_complete_step"
    assert evs[-1]["payload"]["detail"]["last_agent_text"] == "I'm stuck"


@pytest.mark.anyio
async def test_nudge_delivers_continuation_then_short_circuits():
    seen = []

    def cont(last):
        seen.append(last)
        return ("please continue", False)

    inv = SimpleNamespace(continuation_fn=cont)
    sess = FakeSession(
        [
            [EndOfTurn(last_text="need info")],
            [Error(payload={"category": "config", "kind": "interrupted", "message": "x"})],
        ],
        invocation=inv,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert seen == ["need info"]
    assert sess.sent == [("please continue", False)]
    assert sess.interjects == 1
    assert evs[-1] == {
        "type": "error",
        "payload": {"category": "config", "kind": "interrupted", "message": "x"},
    }


@pytest.mark.anyio
async def test_error_item_short_circuits():
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [[Error(payload={"category": "config", "kind": "transcript_invalid", "message": "bad"})]],
        invocation=inv,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs == [
        {
            "type": "error",
            "payload": {"category": "config", "kind": "transcript_invalid", "message": "bad"},
        },
    ]


@pytest.mark.anyio
async def test_thinking_and_warning_recorded_in_order():
    inv = SimpleNamespace(continuation_fn=None)
    elog = InMemoryEventLog()
    sess = FakeSession(
        [[Thinking(text="hmm"), Warning(text="careful"), EndOfTurn(last_text="done")]],
        invocation=inv,
        elog=elog,
    )
    [e async for e in run_agent_loop(inv, sess)]
    logged = [(e["type"], e["payload"]) for e in elog.read()]
    assert logged == [("thinking", {"content": "hmm"}), ("runner_warning", {"message": "careful"})]


@pytest.mark.anyio
async def test_subagent_end_of_turn_yields_agent_result():
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession([[EndOfTurn(last_text="subagent answer")]], invocation=inv, subagent_depth=1)
    evs = [e async for e in run_agent_loop(inv, sess)]
    non_hb = [e for e in evs if e["type"] != "tool_heartbeat"]
    assert non_hb == [{"type": "agent_result", "payload": {"text": "subagent answer"}}]


@pytest.mark.anyio
async def test_dispatch_terminal_agent_result_passes_through_before_send(monkeypatch):
    async def fake_dispatch(**kw):
        yield {"type": "agent_result", "payload": {"status": "Success"}}

    monkeypatch.setattr("imp.agents.agent_loop.dispatch_turn_tool_uses", fake_dispatch)
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [
            [
                ToolRequested(id="t1", name="complete_step", input={"status": "Success"}),
                AwaitingToolResults(),
            ]
        ],
        invocation=inv,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == {"type": "agent_result", "payload": {"status": "Success"}}
    assert sess.sent_results == []  # terminal reached before send_tool_results


@pytest.mark.anyio
async def test_awaiting_tool_results_dispatches_then_loops(monkeypatch):
    captured = {}

    async def fake_dispatch(*, tool_uses, tool_results, **kw):
        captured["tools"] = [(t.name, t.input) for t in tool_uses]
        tool_results.append(ToolResultBlock(tool_use_id="t1", content="ok"))
        yield {"type": "tool_heartbeat", "payload": {}}

    monkeypatch.setattr("imp.agents.agent_loop.dispatch_turn_tool_uses", fake_dispatch)
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [
            [
                ToolRequested(id="t1", name="read_file", input={"path": "a"}),
                AwaitingToolResults(assistant_text="reading"),
            ],
            [Error(payload={"category": "config", "kind": "interrupted", "message": "stop"})],
        ],
        invocation=inv,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert captured["tools"] == [("read_file", {"path": "a"})]
    assert sess.sent_results == [[ToolResultBlock(tool_use_id="t1", content="ok")]]
    assert sess.interjects == 1
    assert evs[-1]["payload"]["kind"] == "interrupted"


@pytest.mark.anyio
async def test_ceiling_tripped_yields_config_error(monkeypatch):
    async def fake_dispatch(*, state, **kw):
        state.ceiling_tripped = True
        yield {"type": "tool_heartbeat", "payload": {}}

    monkeypatch.setattr("imp.agents.agent_loop.dispatch_turn_tool_uses", fake_dispatch)
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [[ToolRequested(id="t1", name="read_file", input={}), AwaitingToolResults()]],
        invocation=inv,
    )
    sess.ceiling = 2
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == {
        "type": "error",
        "payload": {"category": "config", "kind": "tool_call_ceiling_exceeded", "ceiling": 2},
    }


@pytest.mark.anyio
async def test_no_boundary_falls_through_to_interrupted():
    # A turn that yields no AwaitingToolResults/EndOfTurn boundary -> events()
    # exhausts -> break -> trailing interrupted error.
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession([[Thinking(text="just thinking, no boundary")]], invocation=inv)
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == {
        "type": "error",
        "payload": {
            "category": "config",
            "kind": "interrupted",
            "message": "runner cancelled by interrupt",
        },
    }


@pytest.mark.anyio
async def test_handoff_before_nudge_yields_handoff():
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [[EndOfTurn(last_text="crossing threshold")]],
        invocation=inv,
        handoff_before="/tmp/handoff.md",
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == {"type": "handoff", "payload": {"handoff_path": "/tmp/handoff.md"}}


@pytest.mark.anyio
async def test_handoff_after_tools_yields_handoff_before_interject(monkeypatch):
    async def fake_dispatch(*, tool_results, **kw):
        tool_results.append(ToolResultBlock(tool_use_id="t1", content="ok"))
        yield {"type": "tool_heartbeat", "payload": {}}

    monkeypatch.setattr("imp.agents.agent_loop.dispatch_turn_tool_uses", fake_dispatch)
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [[ToolRequested(id="t1", name="read_file", input={}), AwaitingToolResults()]],
        invocation=inv,
        handoff_after=("handoff", "/tmp/h.md"),
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == {"type": "handoff", "payload": {"handoff_path": "/tmp/h.md"}}
    assert sess.interjects == 0  # handoff returns before deliver_turn_boundary_interject


@pytest.mark.anyio
async def test_handoff_after_tools_injected_skips_interject_and_loops(monkeypatch):
    async def fake_dispatch(*, tool_results, **kw):
        tool_results.append(ToolResultBlock(tool_use_id="t1", content="ok"))
        yield {"type": "tool_heartbeat", "payload": {}}

    monkeypatch.setattr("imp.agents.agent_loop.dispatch_turn_tool_uses", fake_dispatch)
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession(
        [
            [ToolRequested(id="t1", name="read_file", input={}), AwaitingToolResults()],
            [Error(payload={"category": "config", "kind": "interrupted", "message": "stop"})],
        ],
        invocation=inv,
        handoff_after=("injected", None),
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    # "injected" path does `continue` WITHOUT calling deliver_turn_boundary_interject
    assert sess.interjects == 0
    assert evs[-1]["payload"]["kind"] == "interrupted"


@pytest.mark.anyio
async def test_guard_trip_on_tool_path_yields_error_after_send(monkeypatch):
    sentinel = {"type": "error", "payload": {"category": "loop", "kind": "repeat"}}
    monkeypatch.setattr(
        "imp.providers.errors.make_error_event",
        lambda err, kind="": sentinel,
    )

    async def fake_dispatch(*, tool_results, **kw):
        tool_results.append(ToolResultBlock(tool_use_id="t1", content="ok"))
        yield {"type": "tool_heartbeat", "payload": {}}

    monkeypatch.setattr("imp.agents.agent_loop.dispatch_turn_tool_uses", fake_dispatch)
    inv = SimpleNamespace(continuation_fn=None)
    guard = TrippingGuard(SimpleNamespace(kind="repeat"))
    sess = FakeSession(
        [[ToolRequested(id="t1", name="read_file", input={}), AwaitingToolResults()]],
        invocation=inv,
        guard=guard,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert guard.observe_calls == 1
    # results sent before observe
    assert sess.sent_results == [[ToolResultBlock(tool_use_id="t1", content="ok")]]
    assert evs[-1] == sentinel


@pytest.mark.anyio
async def test_guard_trip_on_nudge_path_yields_error_after_send(monkeypatch):
    sentinel = {"type": "error", "payload": {"category": "loop", "kind": "repeat"}}
    monkeypatch.setattr(
        "imp.providers.errors.make_error_event",
        lambda err, kind="": sentinel,
    )
    inv = SimpleNamespace(continuation_fn=lambda last: ("continue", False))
    guard = TrippingGuard(SimpleNamespace(kind="repeat"))
    sess = FakeSession(
        [[EndOfTurn(last_text="need more")]],
        invocation=inv,
        guard=guard,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert sess.sent == [("continue", False)]  # nudge sent before observe
    assert guard.observe_calls == 1
    assert evs[-1] == sentinel


@pytest.mark.anyio
async def test_subagent_empty_deliverable_yields_nothing():
    inv = SimpleNamespace(continuation_fn=None)
    sess = FakeSession([[EndOfTurn(last_text="")]], invocation=inv, subagent_depth=1)
    evs = [e async for e in run_agent_loop(inv, sess)]
    non_hb = [e for e in evs if e["type"] != "tool_heartbeat"]
    assert non_hb == []


@pytest.mark.anyio
async def test_awaitable_continuation_is_awaited():
    async def _async_reply():
        return "async continue"

    inv = SimpleNamespace(continuation_fn=lambda last: (_async_reply(), False))
    sess = FakeSession(
        [
            [EndOfTurn(last_text="need info")],
            [Error(payload={"category": "config", "kind": "interrupted", "message": "x"})],
        ],
        invocation=inv,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert sess.sent == [("async continue", False)]
    assert evs[-1]["payload"]["kind"] == "interrupted"
