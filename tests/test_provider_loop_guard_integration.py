"""Fix H: loop_guard trips repeated_failure / no_progress in real provider loops."""

from __future__ import annotations

from dataclasses import replace

import pytest

from imp.agents.loop_guard import DEFAULT_NOPROGRESS_N
from imp.engine.state import InMemoryEventLog
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use
from tests.test_anthropic_runner import _inv

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_anthropic_repeated_prose_trips_repeated_failure(tmp_path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic(
        [response(text("still prose, no tool call"), stop_reason="end_turn")
         for _ in range(DEFAULT_NOPROGRESS_N + 2)])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log),
                  continuation_fn=lambda _last: "please call complete_step")
    events = [e async for e in runner.run(inv)]
    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "loop"
    assert final["payload"]["kind"] == "repeated_failure"


async def test_anthropic_distinct_prose_trips_no_progress(tmp_path) -> None:
    (tmp_path / "art").mkdir()
    responses = [response(text(f"thinking about step {i}"), stop_reason="end_turn")
                 for i in range(DEFAULT_NOPROGRESS_N + 2)]
    fake = FakeAsyncAnthropic(responses)
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log),
                  continuation_fn=lambda _last: "keep going")
    events = [e async for e in runner.run(inv)]
    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "loop"
    assert final["payload"]["kind"] == "no_progress"


async def test_anthropic_interactive_conversation_does_not_trip(tmp_path) -> None:
    # A human-in-the-loop conversation produces many text-only turns (the agent
    # talking, the human replying). These must NOT accumulate no_progress: the
    # human reply each turn proves the agent is not in an autonomous loop.
    (tmp_path / "art").mkdir()
    n_turns = DEFAULT_NOPROGRESS_N + 4   # well past the autonomous threshold
    responses = [response(text(f"agent says {i}"), stop_reason="end_turn")
                 for i in range(n_turns)]
    responses.append(response(
        text("wrapping up"),
        tool_use("c1", "complete_step", {"status": "Success", "note": "done"})))
    fake = FakeAsyncAnthropic(responses)
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = replace(
        _inv(tmp_path, tools=frozenset({"complete_step", "read_file"}),
             event_log=log, interactive=True),
        continuation_fn=lambda _last: "human reply")   # interactive: human keeps talking
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert not any(e["type"] == "error" and e["payload"].get("category") == "loop"
                   for e in events)


async def test_anthropic_healthy_run_never_trips(tmp_path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(tool_use("w1", "write_file", {"path": "a.py", "content": "x"})),
        response(text("done"),
                 tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = replace(
        _inv(tmp_path, tools=frozenset({"complete_step", "write_file"}),
             event_log=log),
        continuation_fn=lambda _last: "keep going")
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert not any(e["type"] == "error" and e["payload"].get("category") == "loop"
                   for e in events)
