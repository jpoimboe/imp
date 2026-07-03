"""In-session nudge recovery for the claude_code provider (post-turn re-query)."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.loop_guard import DEFAULT_REPEAT_K
from imp.engine.state import InMemoryEventLog
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_use
from tests.test_claude_code_runner import _inv

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_malformed_complete_step_nudged_then_recovers(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(tool_use("c1", "complete_step", {"note": "no status"})),
        result(),
        assistant(tool_use("c2", "complete_step",
                           {"status": "Success", "note": "ok"})),
        result(),
    ]
    captured: dict[str, FakeClaudeSDKClient] = {}

    def factory(opts: object) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        captured["c"] = c
        return c

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5",
                                   client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]

    kinds = [e["type"] for e in events]
    assert kinds == ["agent_output"], kinds
    assert events[-1]["payload"] == {"status": "Success", "note": "ok"}
    log_types = [e["type"] for e in log.read()]
    assert log_types.count("output_validation_nudge") == 1
    # The nudge text was re-queried into the SAME session.
    assert any("complete_step call was invalid" in str(q)
               for q in captured["c"].queries)


async def test_repeated_malformed_trips_loop_guard(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted: list[object] = []
    for _ in range(DEFAULT_REPEAT_K + 2):
        scripted.append(
            assistant(tool_use("c", "complete_step", {"note": "no status"})))
        scripted.append(result())

    def factory(opts: object) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5",
                                   client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]

    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "loop"
    assert final["payload"]["kind"] == "repeated_failure"
