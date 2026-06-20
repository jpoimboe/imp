# tests/test_heartbeat.py
"""Heartbeat prerequisite contract (spec: background-execution rev 9).

Pins: exactly one tool_heartbeat per non-terminal tool_use — per ARM
(dispatch success, is_error, task, unknown tool, ceiling backfill) — plus one
per model-API response; consumed-and-dropped in drive_invocation BEFORE both
consumers (event log, pause check); the watchdog window resets per event."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.agent_run import drive_invocation
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.openai import OpenAIAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent_def(tools) -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset(tools), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, tools, *, event_log=None, max_tool_calls=None,
         transcript_dir: str = "step") -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools),
        provider_name="anthropic", model_name="m",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / transcript_dir,
        fs_permissions=(FsRule(operations=("read",), paths=("**",), mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=event_log,
        max_tool_calls=max_tool_calls,
    )


def _hb(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == "tool_heartbeat"]


@pytest.mark.anyio
async def test_one_heartbeat_per_arm_plus_post_api(tmp_path: Path) -> None:
    """Turn 1 carries one tool_use per arm: success (read_file), generic tool
    exception -> is_error (read_file on a missing path), unknown tool, and the
    task arm erroring on an undeclared subagent. Turn 2 is the terminal
    complete_step. Expected: turn1 = 1 post-API + 4 per-tu = 5; turn2 = 1
    post-API + 0 (terminal arm never heartbeats) = 1."""
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeAsyncAnthropic([
        response(
            tool_use("u1", "read_file", {"path": "data.txt"}),
            tool_use("u2", "read_file", {"path": "missing.txt"}),
            tool_use("u3", "no_such_tool", {}),
            tool_use("u4", "task", {"subagent_name": "ghost",
                                    "task_description": "x"}),
        ),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    events = [e async for e in runner.run(
        _inv(tmp_path, {"complete_step", "read_file", "task"}, event_log=log))]
    assert len(_hb(events)) == 6
    assert events[-1]["type"] == "agent_output"
    # heartbeats carry an empty payload (pinned contract)
    assert all(e["payload"] == {} for e in _hb(events))


@pytest.mark.anyio
async def test_ceiling_backfill_arm_heartbeats(tmp_path: Path) -> None:
    """ceiling=1, two tool_uses in one turn: tu1 dispatches, tu2 is ceiling
    backfill — BOTH heartbeat (uniform one-per-non-terminal-tu), then the
    ceiling error terminates. 1 post-API + 2 per-tu = 3."""
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeAsyncAnthropic([
        response(
            tool_use("u1", "read_file", {"path": "data.txt"}),
            tool_use("u2", "read_file", {"path": "data.txt"}),
        ),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    events = [e async for e in runner.run(
        _inv(tmp_path, {"complete_step", "read_file"},
             event_log=InMemoryEventLog(), max_tool_calls=1))]
    assert len(_hb(events)) == 3
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["kind"] == "tool_call_ceiling_exceeded"


@pytest.mark.anyio
async def test_openai_runner_heartbeats_identically(tmp_path: Path) -> None:
    """Parity: the openai sibling emits the same heartbeat pattern."""
    import json

    from tests.openai_fakes import (
        FakeResponsesClient,
        resp_function_call,
        resp_response,
    )
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeResponsesClient([
        resp_response(resp_function_call(
            "u1", "read_file", json.dumps({"path": "data.txt"}))),
        resp_response(resp_function_call(
            "c1", "complete_step", json.dumps({"status": "Success"}))),
    ])
    runner = OpenAIAgentRunner(model_name="m", client_factory=lambda: fake)
    inv = _inv(tmp_path, {"complete_step", "read_file"},
               event_log=InMemoryEventLog())
    events = [e async for e in runner.run(inv)]
    assert len(_hb(events)) == 3  # post-API + tu1, then post-API + terminal
    assert events[-1]["type"] == "agent_output"


# --- drive_invocation consumption -------------------------------------------


class _ScriptedRunner:
    """Fake runner: yields (delay, event) pairs; records consumption depth."""

    def __init__(self, script: list[tuple[float, dict[str, Any]]]) -> None:
        self.script = script
        self.consumed = 0
        self.interrupted = False

    async def interrupt(self) -> None:
        self.interrupted = True

    def run(self, invocation: Any):
        async def gen():
            for delay, ev in self.script:
                await anyio.sleep(delay)
                self.consumed += 1
                yield ev
        return gen()


def _heartbeat() -> dict[str, Any]:
    return {"type": "tool_heartbeat", "payload": {}}


def _output() -> dict[str, Any]:
    return {"type": "agent_output", "payload": {"status": "Success"}}


class _RecLog:
    def __init__(self) -> None:
        self.appended: list[str] = []

    def append(self, *, agent_id, type, payload):
        self.appended.append(type)
        return {"id": "e", "type": type}


@pytest.mark.anyio
async def test_heartbeats_dropped_before_event_log(tmp_path: Path) -> None:
    runner = _ScriptedRunner([(0, _heartbeat()), (0, _heartbeat()), (0, _output())])
    log = _RecLog()
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=None, log=log, emit_events=True)
    assert result.output == {"status": "Success"}
    assert "tool_heartbeat" not in log.appended


class _PauseCoordinator:
    should_pause = True

    def runner_attached(self, fn):
        import contextlib
        return contextlib.nullcontext()

    def drain_interject(self):
        return None


@pytest.mark.anyio
async def test_heartbeat_is_not_a_pause_point(tmp_path: Path) -> None:
    """A set pause flag + arriving heartbeats does NOT end the drive at the
    heartbeat: the generator advances past both heartbeats to the next real
    event (no mid-assistant-turn abandonment / dangling tool_use)."""
    runner = _ScriptedRunner([(0, _heartbeat()), (0, _heartbeat()), (0, _output())])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=_PauseCoordinator(), log=None, emit_events=False)
    assert runner.consumed == 3      # fully drained — did not stop at a heartbeat
    assert result.paused is True     # pause detected at the real event, as today


@pytest.mark.anyio
async def test_heartbeats_reset_watchdog_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cumulative -> per-activity window: total run time exceeds the (shrunk)
    window but every inter-event gap is under it — must NOT stall. The shrunk
    constant still exercises the real race (each __anext__ races fail_after)."""
    monkeypatch.setattr("imp.engine.agent_run.AGENT_INACTIVITY_TIMEOUT_S", 0.5)
    script = [(0.2, _heartbeat()) for _ in range(4)] + [(0.2, _output())]
    runner = _ScriptedRunner(script)   # 1.0 s total, gaps of 0.2 s
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=None, log=None, emit_events=False)
    assert result.output == {"status": "Success"}
    assert result.errored is False


@pytest.mark.anyio
async def test_silent_gap_still_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("imp.engine.agent_run.AGENT_INACTIVITY_TIMEOUT_S", 0.3)
    runner = _ScriptedRunner([(0.1, _heartbeat()), (1.0, _output())])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=None, log=None, emit_events=False)
    assert result.errored is True
    assert result.error is not None and result.error.kind == "stalled"
    assert runner.interrupted is True
