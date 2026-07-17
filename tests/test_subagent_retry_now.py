"""Retry-now in the subagent backoff loop: the sleep is bracketed with
begin/end_backoff_wait and the retry-now latch is consumed after it."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, TextBlock
from imp.cli.signals import InterruptCoordinator
from tests.test_subagent_backoff_visibility import _lead_inv


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
