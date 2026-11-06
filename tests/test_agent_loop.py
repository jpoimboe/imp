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
from imp.cli.narrative import build_tree
from imp.cli.render_text import render_tree
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


class RecordingEvents:
    """One turn's item stream as a PLAIN async iterator, not an async generator:
    nothing closes it implicitly, so `aclose()` is recorded only when the
    consumer closes it."""

    def __init__(self, items, log, turn):
        self._items = list(items)
        self._pos = 0
        self._log = log
        self._turn = turn
        log.append(("open", turn))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pos >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._pos]
        self._pos += 1
        return item

    async def aclose(self):
        self._log.append(("close", self._turn))


class FakeSession:
    def __init__(self, turns, *, subagent_depth=0, invocation=None, elog=None,
                 guard=None, handoff_before=None, handoff_after=("none", None),
                 captured_turns=None, posthoc_ceiling=False):
        self._turns = list(turns)
        self._i = 0
        self._captured_turns = captured_turns   # list | None, one entry per turn
        self._posthoc = posthoc_ceiling
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
        self.stream_log = []      # ("open"|"close", turn index) in call order
        self._last_end_text = ""
        self._handoff_before = handoff_before
        self._handoff_after = handoff_after

    async def open(self):
        self.opened = True

    def cancelled(self):
        return False

    def events(self):
        turn_index = self._i
        turn = self._turns[turn_index]
        self._i += 1
        for it in turn:
            if isinstance(it, EndOfTurn):
                self._last_end_text = it.last_text
        return RecordingEvents(turn, self.stream_log, turn_index)

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

    def captured_complete_step(self):
        if self._captured_turns is None:
            return None
        idx = self._i - 1                        # events() already advanced _i
        return self._captured_turns[idx] if 0 <= idx < len(self._captured_turns) else None

    def posthoc_ceiling_tripped(self):
        return self._posthoc

    def observe_bare_turn(self, last_text):
        # FakeSession models the PUSH contract: events() never self-observes, so
        # the bare-turn LoopGuard feed happens here (prose-only, tools=[]), exactly
        # as PushSession does. (An observe Session overrides this to return None.)
        from imp.agents.loop_guard import TurnRecord
        return self.guard.observe(TurnRecord(
            assistant_text=last_text, tools=[], has_complete_step=False))


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
    tree = build_tree([{"id": "e1", "parent_id": None, "agent_id": None, **evs[-1]}])
    lines = list(render_tree(tree, full=False, color=False, width=200))
    assert ("error: [agent_output_missing] agent finished without reporting "
            "a step result") in lines


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


@pytest.mark.anyio
async def test_observe_captured_complete_step_yields_agent_result(monkeypatch):
    from imp.agents.output_guard import Ok
    monkeypatch.setattr(
        "imp.agents.agent_loop.validate_output_or_nudge",
        lambda args, schema, guard: Ok(payload={"status": "Success"}, recovered=None))
    inv = SimpleNamespace(continuation_fn=None,
                          agent_def=SimpleNamespace(output_schema=object()))
    sess = FakeSession([[EndOfTurn(last_text="done")]], invocation=inv,
                       captured_turns=[{"status": "Success"}])
    evs = [e async for e in run_agent_loop(inv, sess)]
    non_hb = [e for e in evs if e["type"] != "tool_heartbeat"]
    assert non_hb == [{"type": "agent_result", "payload": {"status": "Success"}}]


@pytest.mark.anyio
async def test_observe_captured_recovered_appends_event_then_yields_agent_result(monkeypatch):
    from imp.agents.output_guard import Ok, RecoveryInfo
    monkeypatch.setattr(
        "imp.agents.agent_loop.validate_output_or_nudge",
        lambda args, schema, guard: Ok(
            payload={"status": "Success"},
            recovered=RecoveryInfo(container="c", fields=("f",))))
    elog = InMemoryEventLog()
    inv = SimpleNamespace(continuation_fn=None,
                          agent_def=SimpleNamespace(output_schema=object()))
    sess = FakeSession([[EndOfTurn(last_text="done")]], invocation=inv, elog=elog,
                       captured_turns=[{"status": "Success"}])
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert any(e["type"] == "output_recovered" for e in elog.read())  # recovery event appended
    non_hb = [e for e in evs if e["type"] != "tool_heartbeat"]
    assert non_hb == [{"type": "agent_result", "payload": {"status": "Success"}}]


@pytest.mark.anyio
async def test_observe_captured_nudge_records_event_resends_and_loops(monkeypatch):
    from imp.agents.output_guard import Nudge
    monkeypatch.setattr(
        "imp.agents.agent_loop.validate_output_or_nudge",
        lambda args, schema, guard: Nudge(message="try again",
                                          reason="missing status", attempt=1))
    elog = InMemoryEventLog()
    inv = SimpleNamespace(continuation_fn=None,
                          agent_def=SimpleNamespace(output_schema=object()))
    sess = FakeSession(
        [[EndOfTurn(last_text="bad")],
         [Error(payload={"category": "config", "kind": "interrupted", "message": "x"})]],
        invocation=inv, elog=elog,
        captured_turns=[{"note": "no status"}, None])
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert sess.sent == [("try again", False)]          # re-queried with the nudge
    assert any(e["type"] == "output_validation_nudge" for e in elog.read())
    assert evs[-1]["payload"]["kind"] == "interrupted"  # second turn's Error terminates


@pytest.mark.anyio
async def test_observe_captured_loop_error_yields_error(monkeypatch):
    sentinel = {"type": "error", "payload": {"category": "loop", "kind": "repeated_failure"}}
    monkeypatch.setattr(
        "imp.agents.agent_loop.validate_output_or_nudge",
        lambda args, schema, guard: SimpleNamespace(kind="repeated_failure"))
    monkeypatch.setattr("imp.providers.errors.make_error_event",
                        lambda err, kind="": sentinel)
    inv = SimpleNamespace(continuation_fn=None,
                          agent_def=SimpleNamespace(output_schema=object()))
    sess = FakeSession([[EndOfTurn(last_text="bad")]], invocation=inv,
                       captured_turns=[{"x": 1}])
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == sentinel


@pytest.mark.anyio
async def test_observe_posthoc_ceiling_yields_config_error():
    inv = SimpleNamespace(continuation_fn=None,
                          agent_def=SimpleNamespace(output_schema=object()))
    sess = FakeSession([[EndOfTurn(last_text="over")]], invocation=inv,
                       posthoc_ceiling=True)
    sess.ceiling = 7
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1] == {"type": "error", "payload": {
        "category": "config", "kind": "tool_call_ceiling_exceeded", "ceiling": 7}}


@pytest.mark.anyio
async def test_observe_seam_bare_turn_not_double_fed_by_driver():
    feeds = []
    class ObserveLikeSession(FakeSession):
        def observe_bare_turn(self, last_text):
            return None  # already fed the tools-bearing record inside events()
    inv = SimpleNamespace(continuation_fn=lambda last: ("go on", False))
    guard = SimpleNamespace(observe=lambda rec: feeds.append(rec) or None)
    sess = ObserveLikeSession(
        [[EndOfTurn(last_text="need info")],
         [Error(payload={"category": "config", "kind": "interrupted", "message": "x"})]],
        invocation=inv, guard=guard)
    evs = [e async for e in run_agent_loop(inv, sess)]
    # driver delegated to observe_bare_turn, did NOT self-feed
    assert feeds == []
    assert sess.sent == [("go on", False)]  # continuation still delivered
    assert evs[-1]["payload"]["kind"] == "interrupted"


@pytest.mark.anyio
async def test_turn_stream_closed_before_next_turn_and_on_early_return():
    # Turn 0 ends at an EndOfTurn boundary and is continued; turn 1 short-circuits
    # on a provider Error (an early `return` inside the loop body).
    inv = SimpleNamespace(continuation_fn=lambda last: ("keep going", False))
    sess = FakeSession(
        [
            [EndOfTurn(last_text="turn one")],
            [Error(payload={"category": "config", "kind": "interrupted", "message": "x"})],
        ],
        invocation=inv,
    )
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert evs[-1]["payload"]["kind"] == "interrupted"
    assert sess.sent == [("keep going", False)]
    # Turn 0's stream is closed BEFORE turn 1's events() is requested, and turn 1's
    # stream is closed on the way out of the Error `return`.
    assert sess.stream_log == [("open", 0), ("close", 0), ("open", 1), ("close", 1)]


@pytest.mark.anyio
async def test_cancel_during_continuation_pauses_without_writing_continuation():
    class CancelSession(FakeSession):
        def cancelled(self):
            return getattr(self, "_flag", False)  # False at the while-gate, True after cont()
    inv = SimpleNamespace(continuation_fn=None)
    sess = CancelSession([[EndOfTurn(last_text="need info")]], invocation=inv)
    def cont(last):
        sess._flag = True                         # interrupt lands during resolution
        return ("keep going", False)
    inv.continuation_fn = cont
    evs = [e async for e in run_agent_loop(inv, sess)]
    assert sess.sent == []                         # continuation NOT delivered after cancel
    assert evs[-1] == {"type": "error", "payload": {
        "category": "config", "kind": "interrupted",
        "message": "runner cancelled by interrupt"}}
