"""A complete_step batched after another tool in one API message is rejected, not
terminal. FakeClaudeSDKClient never invokes MCP handlers, so these drive the receive
loop; handler behaviour is in tests/test_claude_wrap.py."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

import imp.agents.classify as classify_mod
import imp.agents.transcript as transcript_mod
from imp.agents.runner import Message, ToolResultBlock
from imp.agents.transcript import COMPLETE_STEP_BATCHED, Transcript, complete_step_tail
from imp.engine.state import InMemoryEventLog
from imp.providers import claude as claude_mod
from imp.providers.claude import ClaudeAgentRunner, ClaudeSession
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    GatedClient,
    assistant,
    result,
    text,
    tool_result,
    tool_use,
    user,
)
from tests.test_claude_complete_terminal import _inv

_BASH = tool_use("b1", "mcp__imp__bash", {"command": "true"})
_FIRST = tool_use("c1", "complete_step", {"status": "Error"})
_SECOND = tool_use("c2", "complete_step", {"status": "Success"})
_CLOSE = result(subtype="error_during_execution", is_error=True)
# List-wrapped and unflagged, unlike the record, so a script alone can't satisfy _assert_rejected.
_HANDLER_ANSWER = [{"type": "text", "text": COMPLETE_STEP_BATCHED}]
_RESULTS = user(tool_result("b1", "ok"), tool_result("c1", _HANDLER_ANSWER))
_FILLER = "The user doesn't want to proceed with this tool use."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class _Run:
    events: list[dict[str, Any]]
    client: Any
    runner: ClaudeAgentRunner
    log: InMemoryEventLog
    messages: list[Message]


async def _run(tmp_path: Path, scripted: list[Any]) -> _Run:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, FakeClaudeSDKClient] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        holder["client"] = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        return holder["client"]

    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(_inv(tmp_path, log))]
    messages = Transcript(tmp_path / "step" / "transcript.jsonl").load()
    return _Run(events, holder["client"], runner, log, messages)


def _journaled(log: InMemoryEventLog, tool_use_id: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in log.read()
            if e["type"] == "tool_result" and e["payload"]["id"] == tool_use_id]


def _stored(messages: list[Message], tool_use_id: str) -> list[ToolResultBlock]:
    return [b for m in messages for b in m.content
            if isinstance(b, ToolResultBlock) and b.tool_use_id == tool_use_id]


def _assert_completed_with(run: _Run, status: str) -> None:
    assert run.client.interrupts == 1
    assert run.events[-1]["type"] == "agent_result"
    assert run.events[-1]["payload"] == {"status": status}


def _assert_rejected(run: _Run, tool_use_id: str) -> None:
    journaled = _journaled(run.log, tool_use_id)
    assert len(journaled) == 1
    assert journaled[0]["content"] == COMPLETE_STEP_BATCHED and journaled[0]["error"] is True
    stored = _stored(run.messages, tool_use_id)
    assert len(stored) == 1
    assert stored[0].content == COMPLETE_STEP_BATCHED and stored[0].is_error is True


def _assert_nothing_rejected(run: _Run) -> None:
    assert not [e for e in run.log.read() if e["type"] == "tool_result"
                and e["payload"]["content"] == COMPLETE_STEP_BATCHED]


@pytest.mark.anyio
async def test_batched_across_fragments_is_rejected_then_rerun(tmp_path: Path) -> None:
    run = await _run(tmp_path, [
        assistant(_BASH, message_id="m"),
        assistant(_FIRST, message_id="m"),
        _RESULTS,
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    _assert_completed_with(run, "Success")
    _assert_rejected(run, "c1")


@pytest.mark.parametrize("sibling", [
    tool_use("b1", "mcp__imp__bash", {"command": "true"}),
    tool_use("b1", "mcp__imp__sub_agent", {"subagent_name": "h", "task_description": "t"}),
    tool_use("b1", "WebSearch", {"query": "imp agents"}),
], ids=["bash", "sub_agent", "web_builtin"])
@pytest.mark.anyio
async def test_batched_in_one_fragment_is_rejected(tmp_path: Path, sibling: Any) -> None:
    run = await _run(tmp_path, [
        assistant(sibling, _FIRST, message_id="m"),
        _RESULTS,
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    _assert_completed_with(run, "Success")
    _assert_rejected(run, "c1")


@pytest.mark.anyio
async def test_fast_sibling_result_between_fragments_still_rejects(tmp_path: Path) -> None:
    run = await _run(tmp_path, [
        assistant(_BASH, message_id="m"),
        user(tool_result("b1", "ok")),
        assistant(_FIRST, message_id="m"),
        user(tool_result("c1", _HANDLER_ANSWER)),
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    _assert_completed_with(run, "Success")
    _assert_rejected(run, "c1")


@pytest.mark.anyio
async def test_lone_complete_step_stays_terminal(tmp_path: Path) -> None:
    run = await _run(tmp_path, [assistant(_SECOND, message_id="m"), _CLOSE])
    _assert_completed_with(run, "Success")
    _assert_nothing_rejected(run)


@pytest.mark.parametrize("fragments", [
    [assistant(_SECOND, _BASH, message_id="m")],
    [assistant(_SECOND, message_id="m"), assistant(_BASH, message_id="m")],
], ids=["one_fragment", "two_fragments"])
@pytest.mark.anyio
async def test_complete_step_first_in_its_message_stays_terminal(
        tmp_path: Path, fragments: list[Any]) -> None:
    run = await _run(tmp_path, [*fragments, _CLOSE])
    _assert_completed_with(run, "Success")
    _assert_nothing_rejected(run)


@pytest.mark.anyio
async def test_same_message_prose_does_not_batch(tmp_path: Path) -> None:
    run = await _run(tmp_path, [
        assistant(text("Recording."), message_id="m"),
        assistant(text("Done."), _SECOND, message_id="m"),
        _CLOSE,
    ])
    _assert_completed_with(run, "Success")
    _assert_nothing_rejected(run)


@pytest.mark.parametrize("fragments", [
    [assistant(_SECOND, _FIRST, message_id="m")],
    [assistant(_SECOND, message_id="m"), assistant(_FIRST, message_id="m")],
], ids=["one_fragment", "two_fragments"])
@pytest.mark.anyio
async def test_later_complete_step_in_an_accepted_message_is_rejected(
        tmp_path: Path, fragments: list[Any]) -> None:
    run = await _run(tmp_path, [*fragments, _CLOSE])
    _assert_completed_with(run, "Success")
    _assert_rejected(run, "c1")


@pytest.mark.anyio
async def test_rejected_call_is_not_guard_progress(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, Any]] = []
    real = ClaudeSession._flush_turn_record

    def spy(self: ClaudeSession) -> Any:
        used = self.tool_calls_used
        rec = real(self)
        if rec is not None:
            seen.append((used, rec))
        return rec

    monkeypatch.setattr(ClaudeSession, "_flush_turn_record", spy)
    await _run(tmp_path, [
        assistant(_BASH, _FIRST, message_id="m"),
        _RESULTS,
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    ((used, rec),) = seen
    assert used == 1
    assert [t.name for t in rec.tools] == ["bash"]
    assert rec.has_complete_step is False


@pytest.mark.anyio
async def test_rejection_is_recorded_before_any_user_message(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    gate = asyncio.Event()
    holder: dict[str, GatedClient] = {}

    def factory(opts: Any) -> GatedClient:
        holder["c"] = GatedClient(opts, scripted_messages=[
            assistant(_BASH, message_id="m"),
            assistant(_FIRST, message_id="m"),
            gate,
            _RESULTS,
            assistant(_SECOND, message_id="m2"),
            _CLOSE,
        ])
        return holder["c"]

    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)

    async def collect() -> list[dict[str, Any]]:
        return [ev async for ev in runner.run(_inv(tmp_path, log))]

    task = asyncio.create_task(collect())
    await wait_until(lambda: "c" in holder and holder["c"].waiting is gate)

    calls = [e["payload"] for e in log.read() if e["type"] == "tool_call"]
    assert ("c1", "complete_step") in [(p["id"], p["name"]) for p in calls]
    journaled = _journaled(log, "c1")
    assert [(p["content"], p["error"]) for p in journaled] == [(COMPLETE_STEP_BATCHED, True)]
    stored = Transcript(tmp_path / "step" / "transcript.jsonl").load()
    assert stored[-1] == Message(role="user", content=(
        ToolResultBlock("c1", COMPLETE_STEP_BATCHED, is_error=True),))
    assert holder["c"].interrupts == 0
    assert runner._complete_requested is False
    assert runner._state.complete_step_called is False
    assert runner._state.complete_step_args == {}

    gate.set()
    with anyio.fail_after(HANG_BACKSTOP):
        await task


@pytest.mark.anyio
async def test_loop_queues_each_calls_verdict_for_its_handler(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    gate = asyncio.Event()
    holder: dict[str, GatedClient] = {}

    def factory(opts: Any) -> GatedClient:
        holder["c"] = GatedClient(opts, scripted_messages=[
            assistant(_SECOND, _FIRST, message_id="m"),
            gate,
            _CLOSE,
        ])
        return holder["c"]

    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)

    async def collect() -> list[dict[str, Any]]:
        return [ev async for ev in runner.run(_inv(tmp_path, InMemoryEventLog()))]

    task = asyncio.create_task(collect())
    await wait_until(lambda: "c" in holder and holder["c"].waiting is gate)

    verdicts = runner._state.complete_verdicts
    assert [verdicts.get_nowait() for _ in range(verdicts.qsize())] == [True, False]

    gate.set()
    with anyio.fail_after(HANG_BACKSTOP):
        await task


@pytest.mark.parametrize("answer", [
    _HANDLER_ANSWER,
    _FILLER,
], ids=["handler", "interrupt_filler"])
@pytest.mark.anyio
async def test_later_answer_for_a_rejected_id_adds_nothing(tmp_path: Path, answer: Any) -> None:
    run = await _run(tmp_path, [
        assistant(_BASH, _FIRST, message_id="m"),
        user(tool_result("c1", answer), tool_result("b1", "ok")),
        user(tool_result("c1", answer)),
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    _assert_rejected(run, "c1")
    assert [b.content for b in _stored(run.messages, "b1")] == ["ok"]
    # The prompt, the record and the sibling's result: the emptied message is gone.
    assert len([m for m in run.messages if m.role == "user"]) == 3
    assert all(m.content for m in run.messages)


@pytest.mark.parametrize("echo", [
    COMPLETE_STEP_BATCHED,
    _HANDLER_ANSWER,
], ids=["str", "list_wrapped"])
@pytest.mark.anyio
async def test_stale_verdict_echo_on_an_accepted_call_is_dropped(
        tmp_path: Path, echo: Any) -> None:
    # A schema-invalid rejected call skips its handler, so the next call's handler gets its False.
    run = await _run(tmp_path, [
        assistant(_BASH, _FIRST, message_id="m"),
        user(tool_result("b1", "ok"),
             tool_result("c1", "Input validation error: x", is_error=True)),
        assistant(_SECOND, message_id="m2"),
        user(tool_result("c2", echo)),
        _CLOSE,
    ])
    _assert_completed_with(run, "Success")
    assert _journaled(run.log, "c2") == []
    assert _stored(run.messages, "c2") == []
    tail = complete_step_tail(run.messages)
    assert tail is not None and tail.id == "c2"


@pytest.mark.anyio
async def test_rejection_copy_on_another_tool_is_kept(tmp_path: Path) -> None:
    run = await _run(tmp_path, [
        assistant(_BASH, message_id="m"),
        user(tool_result("b1", COMPLETE_STEP_BATCHED)),
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    assert [b.content for b in _stored(run.messages, "b1")] == [COMPLETE_STEP_BATCHED]


@pytest.mark.anyio
async def test_result_message_drains_unclaimed_verdicts(tmp_path: Path) -> None:
    run = await _run(tmp_path, [
        assistant(_BASH, _FIRST, message_id="m"),
        _RESULTS,
        assistant(_SECOND, message_id="m2"),
        _CLOSE,
    ])
    assert run.runner._state.complete_verdicts.empty()


@pytest.mark.anyio
async def test_default_ids_across_fragments_stay_terminal(tmp_path: Path) -> None:
    run = await _run(tmp_path, [assistant(_BASH), assistant(_SECOND), _CLOSE])
    _assert_completed_with(run, "Success")
    _assert_nothing_rejected(run)


@pytest.mark.anyio
async def test_default_id_within_one_fragment_is_still_rejected(tmp_path: Path) -> None:
    run = await _run(tmp_path, [
        assistant(_BASH, _FIRST),
        _RESULTS,
        assistant(_SECOND),
        _CLOSE,
    ])
    _assert_completed_with(run, "Success")
    _assert_rejected(run, "c1")


def test_rejection_copy_has_one_definition() -> None:
    assert claude_mod.COMPLETE_STEP_BATCHED is transcript_mod.COMPLETE_STEP_BATCHED
    assert classify_mod.COMPLETE_STEP_BATCHED is transcript_mod.COMPLETE_STEP_BATCHED
