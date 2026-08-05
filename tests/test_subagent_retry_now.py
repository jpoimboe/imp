"""Retry-now in the subagent backoff loop: the sleep is bracketed with
begin/end_backoff_wait and the retry-now latch is consumed after it."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, QuestContext, TextBlock
from imp.agents.subagents import dispatch_task
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.state import JsonlEventLog
from tests.test_subagent_backoff_visibility import _lead_inv
from tests.test_subagents import _agent2, _FakeRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_subagent_backoff_arms_wait_and_consumes_retry_now(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    coord = InterruptCoordinator()
    seen: dict[str, Any] = {}

    async def _fake_sleep(_delay: float, c: Any) -> None:
        seen["armed_during_sleep"] = c.backoff_wait_active
        c.request_retry_now()
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _fake_sleep)

    class _RateLimitThenOkRunner:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            if self.fail:
                yield {"type": "error", "payload": {
                    "category": "rate_limit", "kind": "rate_limit",
                    "message": "429 rate limited"}}
            else:
                yield {"type": "agent_result",
                       "payload": {"status": "Success", "outcome": "done"}}

        async def interrupt(self) -> None:
            ...

    spawns = {"n": 0}

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            i = spawns["n"]
            spawns["n"] += 1
            return _RateLimitThenOkRunner(fail=(i == 0))

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent",
                          {"subagent_name": "coder", "task_description": "edit"})),
        response(tool_use("c1", "complete_step",
                          {"status": "Success", "outcome": "done"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)

    inv = _lead_inv(tmp_path, fake)
    inv = dataclasses.replace(inv, coordinator=coord)
    async for _ in runner.run(inv):
        pass

    assert spawns["n"] == 2                      # failed drive then re-driven to success
    assert seen["armed_during_sleep"] is True    # begin_backoff_wait ran before the sleep
    assert coord.backoff_wait_active is False     # end_backoff_wait ran after
    assert coord.retry_now_requested is False     # loop consumed the latch


def _read_events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _backoff_ctx(tmp_path: Path, *, log: Any, coord: Any) -> ToolContext:
    """First drive fails with rate_limit; dispatch_task takes one backoff sleep, then succeeds on re-spawn."""
    calls = {"n": 0}

    async def _spawn(_inv: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeRunner([{"type": "error", "payload": {
                "category": "rate_limit", "kind": "429", "message": "slow"}}])
        return _FakeRunner([{"type": "agent_result", "payload": {"text": "ok"}}])

    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = coord
    ctx.event_log = log
    ctx.spawn_subagent = _spawn
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf")
    return ctx


@pytest.mark.anyio
async def test_subagent_backoff_ended_reason_retry_now(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_delay: float, c: Any) -> None:
        c.request_retry_now()
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _fake_sleep)

    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = _backoff_ctx(tmp_path, log=log, coord=InterruptCoordinator())

    out = await dispatch_task(ctx, "reviewer", "review it",
                              tool_call_event_id="tc-7")

    assert out == "ok"
    ended = [e for e in _read_events(tmp_path) if e["type"] == "backoff_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"] == {"reason": "retry_now"}
    assert ended[0]["agent_id"] == "sub:tc-7"


@pytest.mark.anyio
async def test_subagent_backoff_ended_reason_elapsed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_delay: float, _c: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _fake_sleep)

    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = _backoff_ctx(tmp_path, log=log, coord=InterruptCoordinator())

    out = await dispatch_task(ctx, "reviewer", "review it",
                              tool_call_event_id="tc-7")

    assert out == "ok"
    ended = [e for e in _read_events(tmp_path) if e["type"] == "backoff_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"] == {"reason": "elapsed"}
    assert ended[0]["agent_id"] == "sub:tc-7"


@pytest.mark.anyio
async def test_subagent_backoff_ended_without_coordinator_is_elapsed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_delay: float, _c: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _fake_sleep)

    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = _backoff_ctx(tmp_path, log=log, coord=None)

    out = await dispatch_task(ctx, "reviewer", "review it",
                              tool_call_event_id="tc-7")

    assert out == "ok"
    ended = [e for e in _read_events(tmp_path) if e["type"] == "backoff_ended"]
    assert len(ended) == 1                      # no latch to read: never retry_now
    assert ended[0]["payload"] == {"reason": "elapsed"}
    assert ended[0]["agent_id"] == "sub:tc-7"


@pytest.mark.anyio
async def test_subagent_backoff_ended_without_event_log_emits_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_delay: float, c: Any) -> None:
        c.request_retry_now()
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _fake_sleep)

    ctx = _backoff_ctx(tmp_path, log=None, coord=InterruptCoordinator())

    out = await dispatch_task(ctx, "reviewer", "review it",
                              tool_call_event_id="tc-7")

    assert out == "ok"                          # the None log raised nothing
    assert not [e for e in _read_events(tmp_path) if e["type"] == "backoff_ended"]
