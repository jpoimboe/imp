from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.agents.tools import Tool
from imp.agents.transcript import RESUME_CONTINUE_NUDGE
from imp.engine.reincarnation import handoff_instruction, handoff_path_for
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.claude import (
    ClaudeAgentRunner,
    _InFlightOutcome,
    _wrap_one,
    correction_increment,
)
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    message_start,
    result,
    text,
    tool_result,
    tool_use,
    user,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "write_file"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="claude",
        model_name="claude-sonnet-4-5", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "wf"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)


# claude-sonnet-4-6's 200k window gives a 160k handoff limit.
_MODEL = "claude-sonnet-4-6"


def _occupancy(tokens: int) -> Any:
    """The open API message's message_start usage, read by the Tier A probe."""
    return message_start("m1", {"input_tokens": tokens})


@pytest.mark.anyio
async def test_threshold_crossing_continues_with_instruction_increment(
        tmp_path: Path) -> None:
    """Crossing the stream-usage handoff limit mid-turn signals interrupt(), lets the
    SDK close the turn balanced (error_during_execution), then continues the LIVE
    session with a plain handoff-instruction increment — NOT a _stream_history
    replay (the regression that doubled the context). The handoff event fires."""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()

    scripted = [
        _occupancy(170_000),
        # Turn 1: an assistant turn carrying a (soon-dangling) tool_use; the open
        # message's stream usage (170k) is past the 160k limit, so the runner
        # interrupts here.
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        # The SDK closes the interrupted turn balanced.
        result(subtype="error_during_execution", is_error=True),
        # Turn 2 (after the plain instruction increment): the agent emits its
        # handoff as plain text; the provider persists it before signalling.
        assistant(text("writing handoff")),
        result(),
    ]

    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    assert not [e for e in events if e["type"] == "error"]
    client = holder["client"]
    assert client.interrupts == 1
    # Regression: the FIRST query streams history (a drained list); EVERY requery
    # after it is a plain increment string. If the handoff path re-streamed
    # history, queries[1:] would contain a list — assert it never does.
    assert isinstance(client.queries[0], list)
    assert all(isinstance(q, str) for q in client.queries[1:])
    # The handoff requery carried the instruction increment as a string.
    assert any("approaching the context limit" in q
               for q in client.queries if isinstance(q, str))
    # The interrupted turn's dangling tool_use ("u1") is balanced for the stash in
    # `messages` only — NOT logged as a real tool_result (it never ran).
    tool_results = [e for e in log.read() if e["type"] == "tool_result"]
    assert not any(e["payload"]["id"] == "u1" for e in tool_results)
    assert hp.exists()
    assert hp.read_text() == "writing handoff"


@pytest.mark.anyio
async def test_threshold_watchdog_does_not_refire_mid_interrupt(
        tmp_path: Path) -> None:
    """After a crossing sets handoff_phase = "interrupt_requested", a second
    assistant message arriving before the balanced close (still above threshold)
    must NOT trigger a second interrupt — interrupt() fires exactly once."""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()

    scripted = [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        # A second assistant message lands before the balanced close, still above
        # threshold; the watchdog must stay quiet because a handoff is in flight.
        assistant(text("still working"),
                  tool_use("u1b", "write_file", {"path": "scratch2", "content": "y"})),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]

    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert holder["client"].interrupts == 1
    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert hp.exists()
    assert hp.read_text() == "writing handoff"


@pytest.mark.anyio
async def test_handoff_reported_even_when_note_missing_and_error(
        tmp_path: Path) -> None:
    """The runner always reports handoff_path on a Tier A handoff turn — it does
    NOT suppress on a missing note or an is_error close. (Failure detection is the
    engine's job, via note existence.)"""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()

    scripted = [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        # Turn 2: the agent produces no final text; the provider writes no note
        # and recovery degrades to transcript-only.
        assistant(text("")),
        result(is_error=True),
    ]

    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    # The agent never wrote the note; the runner still reports it.
    assert not hp.exists()


@pytest.mark.anyio
async def test_below_threshold_completing_turn_interrupts_and_completes(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    scripted = [
        _occupancy(100_000),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)
    # Part A: a completing turn is interrupted regardless of the context
    # threshold — the completion-interrupt, not a handoff.
    assert holder["client"].interrupts == 1


@pytest.mark.anyio
async def test_completing_turn_above_threshold_completes_not_handoff(
        tmp_path: Path) -> None:
    """Completion wins: a turn that calls complete_step is never preempted into
    a handoff, even when stream usage is at/above the limit. Under Part A it
    IS preempted — via the completion-interrupt — so it completes, not hands off.
    Drives the
    PRODUCTION tool name `mcp__imp__complete_step` (the SDK namespaces imp's
    tools); a watch guard comparing the bare name would miss it and wrongly
    interrupt+handoff a completing turn. Requires both the watch guard and the
    runner dual-write to canonicalize via _canon_tool_name."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    scripted = [
        _occupancy(170_000),
        assistant(text("done"),
                  tool_use("c1", "mcp__imp__complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)
    # Part A: the completing turn is interrupted (completion path), not a handoff.
    assert holder["client"].interrupts == 1


@pytest.mark.anyio
async def test_handoff_note_turn_is_flagged_in_the_event_log(tmp_path: Path) -> None:
    # Only the note turn is flagged; the interrupted turn is flushed before the phase is set.
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    scripted = [
        _occupancy(170_000),
        # Turn 1: stream usage 170k is past the 160k limit — the runner interrupts.
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        # Turn 2 (after the instruction increment) is the handoff note itself.
        assistant(text("HANDOFF-SENTINEL: where I left off")),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert len([e for e in events if e["type"] == "handoff"]) == 1
    msgs = [e for e in log.read() if e["type"] == "agent_message"]
    assert len(msgs) >= 2
    # the flagged turn is the note itself, not merely the last message
    assert "HANDOFF-SENTINEL" in str(msgs[-1]["payload"]["raw"])
    assert msgs[-1]["payload"]["handoff"] is True
    assert all("handoff" not in m["payload"] for m in msgs[:-1])


REJECTION = ("The user doesn't want to proceed with this tool use. The tool use "
             "was rejected (eg. if it was a file edit).")

SLOW_ARGS = {"path": "scratch", "content": "x"}


def _passthrough(name, description, schema, annotations=None):  # type: ignore[no-untyped-def]
    """Stub claude_agent_sdk.tool — returns the wrapper body unchanged."""
    def _inner(fn):  # type: ignore[no-untyped-def]
        return fn
    return _inner


def _slow_write_tool(started: asyncio.Event) -> Tool:
    """A real imp tool that is still running when the interrupt lands."""
    async def _fn(ctx: Any, path: str, content: str) -> str:
        started.set()
        await asyncio.sleep(0.2)
        (ctx.project_root / path).write_text(content)
        return f"wrote {len(content)} bytes to {path}"

    return Tool(name="write_file", description="",
                input_schema={"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]},
                fn=_fn)


def _wedged_write_tool(started: asyncio.Event) -> Tool:
    """A tool that never finishes on its own — the only way out is the SDK
    cancelling the handler task, which is what the CLI does on Ctrl-C."""
    async def _fn(ctx: Any, path: str, content: str) -> str:
        started.set()
        await asyncio.sleep(3600)
        return "unreachable"

    return Tool(name="write_file", description="",
                input_schema={"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]},
                fn=_fn)


def _interrupted_script() -> list[Any]:
    """The shape the jibu68 spike observed: imp's tool_use, the SDK's matched
    rejection filler, its interrupt marker, then the balanced error close."""
    return [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("u1", "mcp__imp__write_file", SLOW_ARGS)),
        user(tool_result("u1", REJECTION, is_error=True)),
        user(text("[Request interrupted by user for tool use]")),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]


class _DispatchingClient(FakeClaudeSDKClient):
    """Each interrupt() dispatches imp's real write_file through the wrapper and
    lands once that call is in flight. Like the CLI, the stream yields nothing
    past an interrupt until it has landed."""

    def __init__(self, opts: Any, *, scripted_messages: list[Any],
                 holder: dict[str, Any]) -> None:
        super().__init__(opts, scripted_messages=scripted_messages)
        self._holder = holder
        self._started = asyncio.Event()
        self._landed = asyncio.Event()
        self._landed.set()

    async def interrupt(self) -> None:
        self._landed.clear()
        runner = self._holder["runner"]
        wrapped = _wrap_one("write_file", _slow_write_tool(self._started),
                            runner._ctx, runner._state, _passthrough)
        self._holder.setdefault("calls", []).append(
            asyncio.create_task(wrapped(dict(SLOW_ARGS))))
        await self._started.wait()
        await super().interrupt()
        self._landed.set()

    async def receive_response(self) -> Any:
        async for msg in super().receive_response():
            await self._landed.wait()
            yield msg


@pytest.mark.anyio
async def test_handoff_records_the_real_result_of_a_call_still_in_flight(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real imp tool is dispatched through the wrapper and is still running when
    the threshold interrupt lands; it finishes AFTER the post-interrupt
    ResultMessage. The persisted transcript must carry what the tool really did —
    and the capture sidecar cannot be the source: the spy pins that it did not yet
    exist at the moment imp read it."""
    from imp.providers import claude as cc

    real_load = cc._load_captured_tool_results
    reads: list[bool] = []

    def _spy(d: Path) -> Any:
        reads.append((d / "captured_tool_results.jsonl").exists())
        return real_load(d)

    monkeypatch.setattr(cc, "_load_captured_tool_results", _spy)
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return _DispatchingClient(
            opts, scripted_messages=_interrupted_script(), holder=holder)

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]
    (call,) = holder["calls"]
    await call

    body = (tmp_path / "step" / "transcript.jsonl").read_text()
    rows = [json.loads(x) for x in body.splitlines()]
    results = [b for r in rows if r["role"] == "user"
               for b in r["content"] if b.get("type") == "tool_result"]
    assert [(b["tool_use_id"], b["content"], b["is_error"]) for b in results] == [
        ("u1", "wrote 1 bytes to scratch", False)]
    assert reads[-1] is False              # sidecar unwritten when imp read it
    assert "[Request interrupted by user" not in body
    assert "doesn't want to proceed" not in body


@pytest.mark.anyio
async def test_handoff_substitutes_the_uncertainty_body_when_the_bound_expires(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call that never finishes gets an honest statement of uncertainty on the
    transcript — is_error, but never the SDK's claim of rejection."""
    from imp.agents.transcript import captured_result_key
    from imp.providers import claude as cc

    monkeypatch.setattr(cc, "INFLIGHT_RESULT_BOUND_S", 0.05)
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}
    key = captured_result_key("write_file", SLOW_ARGS)

    class WedgedClient(FakeClaudeSDKClient):
        async def interrupt(self) -> None:
            holder["runner"]._state.register_inflight(key, "write_file")
            await super().interrupt()

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return WedgedClient(opts, scripted_messages=_interrupted_script())

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]

    body = (tmp_path / "step" / "transcript.jsonl").read_text()
    rows = [json.loads(x) for x in body.splitlines()]
    results = [b for r in rows if r["role"] == "user"
               for b in r["content"] if b.get("type") == "tool_result"]
    assert [(b["content"], b["is_error"]) for b in results] == [(
        "imp interrupted this turn; this call was still running and its outcome "
        "was not recorded. Verify the current state before relying on it.", True)]
    assert "doesn't want to proceed" not in body


@pytest.mark.anyio
async def test_handoff_closes_out_the_call_its_bound_gave_up_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The give-up close-out is not a steer-path property: the threshold-crossing
    settle leaves nothing behind either, and it still reports the call before
    dropping it — the run continues past this point with the same state."""
    from imp.agents.transcript import captured_result_key
    from imp.providers import claude as cc

    monkeypatch.setattr(cc, "INFLIGHT_RESULT_BOUND_S", 0.05)
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}
    key = captured_result_key("write_file", SLOW_ARGS)

    class WedgedClient(FakeClaudeSDKClient):
        async def interrupt(self) -> None:
            holder["runner"]._state.register_inflight(key, "write_file")
            await super().interrupt()

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return WedgedClient(opts, scripted_messages=_interrupted_script())

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert runner._state.inflight == {}
    body = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "was not recorded" in body
    assert "doesn't want to proceed" not in body


@pytest.mark.anyio
async def test_handoff_dangling_assistant_tail_stays_balanced(
        tmp_path: Path) -> None:
    """The other tail shape: an assistant turn with a tool_use and no user turn
    after it. Adding the marker repair ahead of the dangling repair must leave this
    one balanced — every tool_use still has its tool_result."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    scripted = [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    [ev async for ev in runner.run(_inv(tmp_path, log))]

    rows = [json.loads(x) for x in
            (tmp_path / "step" / "transcript.jsonl").read_text().splitlines()]
    calls = [b["id"] for r in rows if r["role"] == "assistant"
             for b in r["content"] if b.get("type") == "tool_use"]
    results = [b["tool_use_id"] for r in rows if r["role"] == "user"
               for b in r["content"] if b.get("type") == "tool_result"]
    assert calls == ["u1"] and results == ["u1"]


def _naive_note_from(prompt: str) -> str:
    """Stand-in for the model that authors the handoff note: it reports the newest
    account of the interrupted call it was handed. With no correction increment the
    only account in its context is the SDK's rejection filler — which is exactly the
    note the jibu68 session wrote."""
    for line in reversed(prompt.splitlines()):
        if line.startswith("- write_file:"):
            return f"handoff: write_file — {line.split(':', 1)[1].strip()}"
    return "handoff: write_file was rejected by the user; nothing was written"


@pytest.mark.anyio
async def test_handoff_note_is_written_from_imps_real_result(
        tmp_path: Path) -> None:
    """The whole point of Channel 1: the note-authoring turn is driven by the string
    imp queries against the LIVE client, so correcting that string corrects the note.
    The increment rides as a plain string increment, never a history replay."""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}

    class NoteWritingClient(_DispatchingClient):
        async def query(self, prompt: Any) -> None:
            await super().query(prompt)
            if isinstance(prompt, str) and "approaching the context limit" in prompt:
                # The cursor sits on the handoff turn's assistant message; author it
                # from what the model was actually told.
                self._scripted[self._pos] = assistant(
                    text(_naive_note_from(prompt)))

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = NoteWritingClient(
            opts, scripted_messages=_interrupted_script(), holder=holder)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]
    (call,) = holder["calls"]
    await call

    client = holder["client"]
    requery = client.queries[1]
    assert requery.startswith(
        "[imp] This turn was interrupted while the calls below were still "
        "running.")
    assert "- write_file: wrote 1 bytes to scratch" in requery
    assert "You are approaching the context limit" in requery
    assert "doesn't want to proceed" not in requery
    # Still a plain string increment — the all-strings requery invariant.
    assert all(isinstance(q, str) for q in client.queries[1:])
    # The note the successor inherits reports the real outcome, not a rejection.
    assert hp.read_text() == "handoff: write_file — wrote 1 bytes to scratch"
    # Journaled once, in the instruction's own engine_message/continuation.
    cont = [e for e in log.read() if e["type"] == "engine_message"
            and e["payload"].get("subtype") == "continuation"]
    assert len(cont) == 1
    assert ("- write_file: wrote 1 bytes to scratch"
            in cont[0]["payload"]["content"][0]["text"])


@pytest.mark.anyio
async def test_handoff_increment_states_uncertainty_when_the_bound_expires(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call still wedged when the bound expires is named in the increment with the
    honest uncertainty sentence — never the SDK's claim of rejection."""
    from imp.agents.transcript import captured_result_key
    from imp.providers import claude as cc

    monkeypatch.setattr(cc, "INFLIGHT_RESULT_BOUND_S", 0.05)
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}
    key = captured_result_key("write_file", SLOW_ARGS)

    class WedgedClient(FakeClaudeSDKClient):
        async def interrupt(self) -> None:
            holder["runner"]._state.register_inflight(key, "write_file")
            await super().interrupt()

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = WedgedClient(opts, scripted_messages=_interrupted_script())
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]

    requery = holder["client"].queries[1]
    assert ("- write_file: imp interrupted this turn; this call was still running "
            "and its outcome was not recorded. Verify the current state before "
            "relying on it.") in requery
    assert "doesn't want to proceed" not in requery


@pytest.mark.anyio
async def test_handoff_with_nothing_in_flight_queries_the_bare_instruction(
        tmp_path: Path) -> None:
    """No imp tool was in flight, so there is nothing to correct: the requery is the
    handoff instruction and nothing else, and the log gains no extra event."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    scripted = [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert holder["client"].queries[1] == handoff_instruction()
    cont = [e for e in log.read() if e["type"] == "engine_message"
            and e["payload"].get("subtype") == "continuation"]
    assert len(cont) == 1


@pytest.mark.anyio
async def test_handoff_event_log_carries_imps_real_result_not_the_filler(
        tmp_path: Path) -> None:
    """Channel 2: `imp events` is what reincarnation points the reborn session at,
    so the filler must never reach it. The log carries one call and one result for
    u1 — imp's own — and the narrative reads them back as that call's outcome."""
    from imp.cli.narrative import NarrativeBuilder

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return _DispatchingClient(
            opts, scripted_messages=_interrupted_script(), holder=holder)

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]
    (call,) = holder["calls"]
    await call

    events = list(log.read())
    assert REJECTION not in json.dumps(events)
    assert not runner._state.withheld
    results = [e for e in events if e["type"] == "tool_result"]
    assert [(e["payload"]["id"], e["payload"]["content"], e["payload"]["error"])
            for e in results] == [("u1", "wrote 1 bytes to scratch", False)]
    # Same duration source as the streaming append it replaces: imp's own tools
    # carry none on either path, so the key stays absent.
    assert "duration_ms" not in results[0]["payload"]
    # The call it answers is journaled with it, adjacent and in order.
    types = [e["type"] for e in events]
    i = types.index("tool_call")
    assert types[i:i + 2] == ["tool_call", "tool_result"]
    assert events[i]["payload"] == {
        "id": "u1", "name": "write_file", "input": SLOW_ARGS}
    # Read back by the real consumer: one call, correctly named, with its outcome —
    # not the orphan node an unpaired result renders as.
    builder = NarrativeBuilder()
    burst: Any = None
    for e in events:
        builder.add(e)
        if e["type"] == "tool_result":
            burst = builder.current_burst
    assert burst is not None
    assert [(c.call_id, c.name, c.result, c.is_error) for c in burst.calls] == [
        ("u1", "write_file", "wrote 1 bytes to scratch", False)]


@pytest.mark.anyio
async def test_a_filler_imp_cannot_account_for_stays_in_the_event_log(
        tmp_path: Path) -> None:
    """Suppression is narrow. Nothing was dispatched through imp's wrapper here, so
    imp has no better account of u1 than the SDK's — dropping it would lose the
    call's only record."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=_interrupted_script())

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)

    evs = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert len([e for e in evs if e["type"] == "handoff"]) == 1
    results = [e for e in log.read() if e["type"] == "tool_result"]
    assert [(e["payload"]["id"], e["payload"]["content"]) for e in results] == [
        ("u1", REJECTION)]


@pytest.mark.anyio
async def test_a_complete_step_after_the_crossing_keeps_the_withheld_result(
        tmp_path: Path) -> None:
    """complete_step preemption wins over the handoff close, so no bounded await
    runs and nothing corrects u1. The withhold must not become a deletion: the spec
    leaves this site exactly as it is, which means the filler goes back — call and
    result both present, and nothing left pending on the run state."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}
    scripted = [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("u1", "mcp__imp__write_file", SLOW_ARGS)),
        user(tool_result("u1", REJECTION, is_error=True)),
        user(text("[Request interrupted by user for tool use]")),
        assistant(text("done"),
                  tool_use("c1", "mcp__imp__complete_step",
                           {"status": "Success"})),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("trailing")),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return _DispatchingClient(
            opts, scripted_messages=scripted, holder=holder)

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    [ev async for ev in runner.run(_inv(tmp_path, log))]
    assert len(holder["calls"]) == 2
    for task in holder["calls"]:
        await task

    events = list(log.read())
    results = [(e["payload"]["id"], e["payload"]["content"],
                e["payload"]["error"]) for e in events
               if e["type"] == "tool_result"]
    assert results == [("u1", REJECTION, True)]
    assert ("u1", "write_file") in [
        (e["payload"]["id"], e["payload"]["name"]) for e in events
        if e["type"] == "tool_call"]
    assert not runner._state.withheld
    # The transcript keeps the filler here too — this site is unchanged by design.
    assert REJECTION in (tmp_path / "step" / "transcript.jsonl").read_text()


@pytest.mark.anyio
async def test_a_killed_bash_result_on_the_crossing_turn_emits_tool_killed(
        tmp_path: Path) -> None:
    """The crossing returns before the call reaches _pending_tools, so the kill's
    tool name comes from the transcript's tool_use."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    scripted = [
        _occupancy(170_000),
        assistant(text("working"),
                  tool_use("tu1", "mcp__imp__bash", {"command": "sleep 45"})),
        user(tool_result("tu1", '{"returncode": -15, "stdout": ""}')),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]
    runner = ClaudeAgentRunner(
        model_name=_MODEL,
        client_factory=lambda opts: FakeClaudeSDKClient(
            opts, scripted_messages=scripted))

    [ev async for ev in runner.run(_inv(tmp_path, log))]

    events = list(log.read())
    i = next(i for i, e in enumerate(events)
             if e["type"] == "tool_result" and e["payload"]["id"] == "tu1")
    assert events[i + 1]["type"] == "tool_killed"
    assert events[i + 1]["payload"] == {"id": "tu1", "name": "bash", "signal": 15}


@pytest.mark.anyio
async def test_a_fatal_tool_error_after_the_crossing_keeps_the_withheld_result(
        tmp_path: Path) -> None:
    """A fatal tool error returns from events() BEFORE _is_terminal runs, so neither
    drain on that method is reached. The run's own teardown is the last point that
    can honour the deferral; without it the call's only record is deleted."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}

    class FatalClient(_DispatchingClient):
        async def receive_response(self) -> Any:
            seen = 0
            async for msg in super().receive_response():
                yield msg
                seen += 1
                if seen == 3:
                    # Resumed once the runner has processed the filler, so it is
                    # withheld now and the fatal lands before any ResultMessage —
                    # the window _is_terminal never sees. This is the tuple
                    # _wrap_one itself writes when a tool raises FatalToolError.
                    holder["runner"]._state.fatal_tool_error = (
                        "create_and_enter_worktree", "worktree already exists")

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FatalClient(
            opts, scripted_messages=_interrupted_script(), holder=holder)

    runner = ClaudeAgentRunner(
        model_name=_MODEL, client_factory=factory)
    holder["runner"] = runner

    evs = [ev async for ev in runner.run(_inv(tmp_path, log))]
    assert holder.get("calls")
    for task in holder["calls"]:
        await task

    assert [e["payload"]["kind"] for e in evs if e["type"] == "error"] == [
        "fatal_tool_error"]
    events = list(log.read())
    assert [(e["payload"]["id"], e["payload"]["content"], e["payload"]["error"])
            for e in events if e["type"] == "tool_result"] == [
        ("u1", REJECTION, True)]
    assert ("u1", "write_file") in [
        (e["payload"]["id"], e["payload"]["name"]) for e in events
        if e["type"] == "tool_call"]
    assert not runner._state.withheld


class _RecordingClient(FakeClaudeSDKClient):
    """Records runner._handoff_requested at each interrupt() call."""

    def __init__(self, opts: Any, *, scripted_messages: list[Any],
                 holder: dict[str, Any]) -> None:
        super().__init__(opts, scripted_messages=scripted_messages)
        self._holder = holder

    async def interrupt(self) -> None:
        self._holder.setdefault("requested", []).append(
            self._holder["runner"]._handoff_requested)
        await super().interrupt()


@pytest.mark.anyio
async def test_stream_usage_at_the_handoff_limit_requests_handoff(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}
    scripted = [
        # 150k input + 10k cache read = 160k: exactly the limit, cache included.
        message_start("m1", {"input_tokens": 150_000,
                             "cache_read_input_tokens": 10_000}),
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]
    runner = ClaudeAgentRunner(
        model_name=_MODEL,
        client_factory=lambda opts: _RecordingClient(
            opts, scripted_messages=scripted, holder=holder))
    holder["runner"] = runner

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert holder.get("requested") == [True]
    assert len([e for e in events if e["type"] == "handoff"]) == 1


@pytest.mark.anyio
async def test_stream_usage_below_the_handoff_limit_does_not_request_handoff(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, Any] = {}
    scripted = [
        message_start("m1", {"input_tokens": 149_999,
                             "cache_read_input_tokens": 10_000}),
        # Probed (no complete_step) at 159,999 — one under the limit.
        assistant(text("working"), message_id="m1"),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m1"),
        result(),
    ]
    runner = ClaudeAgentRunner(
        model_name=_MODEL,
        client_factory=lambda opts: _RecordingClient(
            opts, scripted_messages=scripted, holder=holder))
    holder["runner"] = runner

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    # The only interrupt is the complete_step preemption, with no handoff asked.
    assert holder.get("requested") == [False]
    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)


def _steer_script() -> tuple[list[Any], list[Any]]:
    """The steer arrival: imp's tool_use, the SDK's matched rejection filler and
    its interrupt marker before the balanced error close, then the steered turn."""
    before_result = [
        assistant(text("working"),
                  tool_use("u1", "mcp__imp__write_file", SLOW_ARGS)),
        user(tool_result("u1", REJECTION, is_error=True)),
        user(text("[Request interrupted by user for tool use]")),
    ]
    after_steer = [
        assistant(text("checking the other file"),
                  tool_use("c1", "mcp__imp__complete_step", {"status": "Success"})),
        result(),
    ]
    return before_result, after_steer


async def _run_steer(tmp_path: Path, log: InMemoryEventLog, reply: str, *,
                     cancel_in_flight: bool = False,
                     marks: list[float] | None = None) -> Any:
    """Drive a Ctrl-C steer whose interrupt lands while one of imp's own tools is
    mid-flight, and return the live client once the run has finished.

    cancel_in_flight models the CLI cancelling the outstanding tools/call: the
    dispatched tool never finishes on its own and its task is cancelled before the
    fake's stream is released, so the cancellation precedes the SDK's filler the way
    it does in production. marks collects (interrupt, steer solicit) timestamps.
    """
    from imp.cli.signals import InterruptCoordinator
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    before_result, after_steer = _steer_script()
    coord = InterruptCoordinator()
    holder: dict[str, Any] = {}
    started = asyncio.Event()

    class DispatchingCooperativeClient(CooperativeClient):
        async def interrupt(self) -> None:
            # Fires twice: the steer, then Part A preempting the steered turn's
            # complete_step. Only the first lands mid-tool.
            if "call" not in holder:
                runner = holder["runner"]
                tool = (_wedged_write_tool(started) if cancel_in_flight
                        else _slow_write_tool(started))
                wrapped = _wrap_one("write_file", tool, runner._ctx,
                                    runner._state, _passthrough)
                holder["call"] = asyncio.create_task(wrapped(dict(SLOW_ARGS)))
                await started.wait()
                if cancel_in_flight:
                    holder["call"].cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await holder["call"]
            await super().interrupt()

    def _user_input(_prompt: str) -> str:
        if marks is not None:
            marks.append(time.monotonic())
        return reply

    def factory(opts: Any) -> Any:
        c = DispatchingCooperativeClient(opts, after_steer=after_steer,
                                         before_result=before_result)
        holder["c"] = c
        return c

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=_user_input)
    holder["runner"] = runner
    inv = replace(_inv(tmp_path, log), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    async def drive() -> None:
        async for _ev in runner.run(inv):
            pass

    task = asyncio.create_task(drive())
    for _ in range(2000):
        if "c" in holder and holder["c"].queries:
            break
        await asyncio.sleep(0.01)
    if marks is not None:
        marks.append(time.monotonic())
    await coord.signal_pause()
    await asyncio.wait_for(task, timeout=60)
    with contextlib.suppress(asyncio.CancelledError):
        await holder["call"]
    return holder["c"]


@pytest.mark.anyio
async def test_steer_corrects_all_three_channels_for_a_call_in_flight(
        tmp_path: Path) -> None:
    """AC 13: the steer path owes the same three corrections as the handoff path —
    the live session's next query, the event log, and the transcript."""
    log = InMemoryEventLog()
    client = await _run_steer(tmp_path, log, "go check the other file instead")

    # Channel 1: the steered query carries the increment, still a plain string —
    # the first query streams history, every requery after it is an increment.
    assert isinstance(client.queries[0], list)
    assert all(isinstance(q, str) for q in client.queries[1:])
    assert client.queries[-1] == correction_increment([
        _InFlightOutcome(key="k", name="write_file",
                         content="wrote 1 bytes to scratch",
                         is_error=False)]) + "go check the other file instead"

    # Channel 2: the log carries imp's own account, once, with its call.
    events = list(log.read())
    assert REJECTION not in json.dumps(events)
    results = [e for e in events if e["type"] == "tool_result"]
    assert [(e["payload"]["id"], e["payload"]["content"], e["payload"]["error"])
            for e in results] == [("u1", "wrote 1 bytes to scratch", False)]
    assert "duration_ms" not in results[0]["payload"]
    assert [e["payload"]["id"] for e in events
            if e["type"] == "tool_call"].count("u1") == 1
    cont = [e for e in events if e["type"] == "engine_message"
            and e["payload"].get("subtype") == "continuation"]
    assert len(cont) == 1
    assert cont[0]["payload"]["content"][0]["text"].startswith(
        "[imp] This turn was interrupted")

    # Channel 3: the transcript's result for u1 is the real one, marker collapsed.
    body = (tmp_path / "step" / "transcript.jsonl").read_text()
    rows = [json.loads(x) for x in body.splitlines()]
    assert [(b["tool_use_id"], b["content"], b["is_error"])
            for r in rows if r["role"] == "user"
            for b in r["content"] if b.get("type") == "tool_result"] == [
        ("u1", "wrote 1 bytes to scratch", False)]
    assert "[Request interrupted by user" not in body
    assert REJECTION not in body


@pytest.mark.anyio
async def test_steer_empty_reply_carries_the_increment_before_the_nudge(
        tmp_path: Path) -> None:
    """The other steer arm: an empty reply continues on the nudge, which the
    correction must precede rather than replace."""
    log = InMemoryEventLog()
    client = await _run_steer(tmp_path, log, "")

    q = client.queries[-1]
    assert q.startswith("[imp] This turn was interrupted")
    assert "- write_file: wrote 1 bytes to scratch" in q
    assert q.endswith(RESUME_CONTINUE_NUDGE)


@pytest.mark.anyio
async def test_steer_returns_promptly_when_the_handler_is_cancelled(
        tmp_path: Path) -> None:
    """The fabe76 hang end to end: the CLI cancels the in-flight tools/call, so the
    wrapper unwinds on CancelledError and nothing can ever complete its future. The
    prompt must not wait out the 30 s bound — and all three corrections must still
    land, with imp's uncertainty body in place of the client's filler."""
    log = InMemoryEventLog()
    marks: list[float] = []
    client = await _run_steer(tmp_path, log, "never mind",
                              cancel_in_flight=True, marks=marks)

    assert len(marks) >= 2
    assert marks[1] - marks[0] < 2.0

    uncertain = (
        "imp interrupted this turn; this call was still running and its outcome "
        "was not recorded. Verify the current state before relying on it.")
    events = list(log.read())
    assert REJECTION not in json.dumps(events)
    assert [(e["payload"]["id"], e["payload"]["content"], e["payload"]["error"])
            for e in events if e["type"] == "tool_result"] == [
        ("u1", uncertain, True)]
    assert [e["payload"]["id"] for e in events
            if e["type"] == "tool_call"].count("u1") == 1
    assert client.queries[-1] == correction_increment([
        _InFlightOutcome(key="k", name="write_file", content=uncertain,
                         is_error=True)]) + "never mind"


@pytest.mark.anyio
async def test_steer_kills_before_waiting_so_bash_reports_its_real_death(
        tmp_path: Path) -> None:
    """AC 15: Ctrl-C during a long bash. The kill runs ahead of the bounded await,
    so the recorded result is the real killed-process one and the steer prompt is
    reached in the kill's own escalation, not after the 30s bound."""
    from imp.agents.tools import get_registry
    from imp.agents.tools.context import terminate_tool_processes
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    args = {"command": "echo hello; sleep 120"}
    before_result = [
        assistant(text("running"), tool_use("u1", "mcp__imp__bash", args)),
        user(tool_result("u1", REJECTION, is_error=True)),
        user(text("[Request interrupted by user for tool use]")),
    ]
    after_steer = [
        assistant(text("ok"),
                  tool_use("c1", "mcp__imp__complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, Any] = {}
    reached: list[float] = []

    def _reply(_prompt: str) -> str:
        reached.append(time.monotonic())
        return "never mind"

    def factory(opts: Any) -> Any:
        c = CooperativeClient(opts, after_steer=after_steer,
                              before_result=before_result)
        holder["c"] = c
        return c

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=_reply)

    async def drive() -> None:
        async for _ev in runner.run(_inv(tmp_path, log)):
            pass

    task = asyncio.create_task(drive())
    for _ in range(2000):
        if "c" in holder and holder["c"].queries:
            break
        await asyncio.sleep(0.01)

    wrapped = _wrap_one("bash", get_registry()["bash"],
                        runner._ctx, runner._state, _passthrough)
    call = asyncio.create_task(wrapped(dict(args)))
    try:
        for _ in range(2000):
            if runner._ctx.running_processes:
                break
            await asyncio.sleep(0.01)
        assert runner._ctx.running_processes
        # wait for echo output before killing so the kill can't race it
        from imp.agents.tools.bg import _bg_dir
        bg = _bg_dir(runner._ctx)
        hello_seen = False
        for _ in range(2000):
            for f in bg.glob("fg-*.out"):
                try:
                    if b"hello" in f.read_bytes():
                        hello_seen = True
                        break
                except OSError:
                    pass
            if hello_seen:
                break
            await asyncio.sleep(0.01)
        assert hello_seen, "echo output never appeared before interrupt"
        t0 = time.monotonic()
        await runner.interrupt()
        await asyncio.wait_for(task, timeout=60)
        await call
    finally:
        await terminate_tool_processes(runner._ctx)

    assert reached and reached[0] - t0 < 15
    results = [e for e in log.read() if e["type"] == "tool_result"]
    assert [e["payload"]["id"] for e in results] == ["u1"]
    content = results[0]["payload"]["content"]
    assert content != REJECTION
    assert "was not recorded" not in content
    assert json.loads(content)["returncode"] < 0
    assert json.loads(content)["stdout"] == "hello\n"


@pytest.mark.anyio
async def test_steer_renders_a_bash_killed_by_the_interrupt_on_its_own_call(
        tmp_path: Path) -> None:
    """The kill is journaled with the call's own result, so the narrative marks the
    original row [killed] instead of a phantom call after the reply."""
    from imp.agents.tools import get_registry
    from imp.cli.narrative import build_tree
    from imp.cli.render_text import render_tree
    from imp.cli.signals import InterruptCoordinator
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    args = {"command": "sleep 45"}
    before_result = [
        assistant(text("running"), tool_use("u1", "mcp__imp__bash", args)),
        user(tool_result("u1", REJECTION, is_error=True)),
        user(text("[Request interrupted by user for tool use]")),
    ]
    after_steer = [
        message_start("m2", {"input_tokens": 10}),
        assistant(text("The sleep was killed, so I stopped."),
                  tool_use("c1", "mcp__imp__complete_step", {"status": "Success"})),
        result(),
    ]
    started = asyncio.Event()

    async def _killed_bash(ctx: Any, **_kw: Any) -> str:
        started.set()
        await asyncio.sleep(0.2)
        return json.dumps({"stdout": "", "stderr": "", "returncode": -15})

    bash = replace(get_registry()["bash"], fn=_killed_bash)
    coord = InterruptCoordinator()
    holder: dict[str, Any] = {}

    class DispatchingCooperativeClient(CooperativeClient):
        async def interrupt(self) -> None:
            if "call" not in holder:
                runner = holder["runner"]
                wrapped = _wrap_one("bash", bash, runner._ctx, runner._state,
                                    _passthrough)
                holder["call"] = asyncio.create_task(wrapped(dict(args)))
                await started.wait()
            await super().interrupt()

    def factory(opts: Any) -> Any:
        holder["c"] = DispatchingCooperativeClient(
            opts, after_steer=after_steer, before_result=before_result)
        return holder["c"]

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=lambda _prompt: "stop and tell me")
    holder["runner"] = runner
    inv = replace(_inv(tmp_path, log), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    async def drive() -> None:
        async for _ev in runner.run(inv):
            pass

    task = asyncio.create_task(drive())
    for _ in range(2000):
        if "c" in holder and holder["c"].queries:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("client never received a query")
    await coord.signal_pause()
    await asyncio.wait_for(task, timeout=60)
    await holder["call"]

    events = [{"id": "s0", "agent_id": None, "type": "step_started",
               "payload": {"step": "s", "kind": "agent", "phase": "p",
                           "model": "m", "provider": "claude"}}, *log.read()]

    def _render(full: bool) -> list[str]:
        return list(render_tree(build_tree(events), full=full, color=False, width=80))

    lean = _render(False)
    reply = lean.index("> stop and tell me")
    answer = lean.index("• The sleep was killed, so I stopped.")
    [recap] = [ln for ln in lean[:reply] if " call" in ln]
    assert re.fullmatch(r"  1 call · bash( · (\d+m )?\d+s)?", recap)
    assert not [ln for ln in lean[reply:answer] if " call" in ln]

    full = _render(True)
    reply = full.index("> stop and tell me")
    answer = full.index("• The sleep was killed, so I stopped.")
    assert [ln for ln in full[:reply] if ln.startswith("  · ")] == [
        "  · bash [killed]  command=sleep 45"]
    assert full[:reply].count("▾ tools") == 1
    assert "▾ tools" not in full[reply:answer]
