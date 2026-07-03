"""In-session nudge recovery for the in-process providers (anthropic/openai).

A malformed top-level complete_step is recovered in the SAME run: the agent
re-emits a valid complete_step and the step completes, with exactly one
output_validation_nudge event and no terminal error. Repeated malformed calls
trip the loop guard (pause-to-human) instead of looping forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.loop_guard import DEFAULT_REPEAT_K
from imp.engine.state import InMemoryEventLog
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.openai import OpenAIAgentRunner
from tests._events import control_events
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use
from tests.openai_fakes import (
    FakeResponsesClient,
    resp_function_call,
    resp_response,
)
from tests.test_anthropic_runner import _inv as _anthropic_inv
from tests.test_openai_runner import _inv as _openai_inv

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_anthropic_malformed_complete_step_nudged_then_recovers(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        # Turn 1: missing required 'status' -> nudge.
        response(tool_use("c1", "complete_step", {"note": "no status"})),
        # Turn 2: valid -> agent_output.
        response(tool_use("c2", "complete_step",
                          {"status": "Success", "note": "ok"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    events = [e async for e in runner.run(_anthropic_inv(tmp_path, event_log=log))]

    ctrl = control_events(events)
    assert [e["type"] for e in ctrl] == ["agent_output"]
    assert ctrl[-1]["payload"] == {"status": "Success", "note": "ok"}
    log_types = [e["type"] for e in log.read()]
    assert log_types.count("output_validation_nudge") == 1
    assert "error" not in log_types


async def test_anthropic_repeated_malformed_trips_loop_guard(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(tool_use(f"c{i}", "complete_step", {"note": "no status"}))
        for i in range(DEFAULT_REPEAT_K + 2)
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    events = [e async for e in runner.run(_anthropic_inv(tmp_path, event_log=log))]

    final = control_events(events)[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "loop"
    assert final["payload"]["kind"] == "repeated_failure"


async def test_openai_malformed_complete_step_nudged_then_recovers(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(resp_function_call("c1", "complete_step",
                                         '{"note": "no status"}')),
        resp_response(resp_function_call("c2", "complete_step",
                                         '{"status": "Success", "note": "ok"}')),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    events = [e async for e in runner.run(_openai_inv(tmp_path, event_log=log))]

    ctrl = control_events(events)
    assert [e["type"] for e in ctrl] == ["agent_output"]
    assert ctrl[-1]["payload"] == {"status": "Success", "note": "ok"}
    log_types = [e["type"] for e in log.read()]
    assert log_types.count("output_validation_nudge") == 1
    assert "error" not in log_types


async def test_openai_repeated_malformed_trips_loop_guard(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(resp_function_call(f"c{i}", "complete_step",
                                         '{"note": "no status"}'))
        for i in range(DEFAULT_REPEAT_K + 2)
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    events = [e async for e in runner.run(_openai_inv(tmp_path, event_log=log))]

    final = control_events(events)[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "loop"
    assert final["payload"]["kind"] == "repeated_failure"
