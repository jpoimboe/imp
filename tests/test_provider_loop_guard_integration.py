"""Fix H: loop_guard trips repeated_failure / no_progress in real provider loops."""

from __future__ import annotations

from dataclasses import replace

import pytest

from imp.agents.loop_guard import DEFAULT_NOPROGRESS_N, DEFAULT_REPEAT_K
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
                  continuation_fn=lambda _last: ("please call complete_step", False))
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
                  continuation_fn=lambda _last: ("keep going", False))
    events = [e async for e in runner.run(inv)]
    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "loop"
    assert final["payload"]["kind"] == "no_progress"


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
        continuation_fn=lambda _last: ("keep going", False))
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert not any(e["type"] == "error" and e["payload"].get("category") == "loop"
                   for e in events)


async def test_anthropic_failed_tool_preview_reaches_full_summary(tmp_path) -> None:
    (tmp_path / "art").mkdir()
    # K identical failing read_file turns → repeated_failure; the failed result's
    # text must populate result_preview → full_summary = "failed read_file: <preview>".
    fake = FakeAsyncAnthropic([
        response(tool_use(f"r{i}", "read_file", {"path": "does-not-exist.txt"}))
        for i in range(DEFAULT_REPEAT_K + 1)])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = _inv(tmp_path, event_log=log)
    events = [e async for e in runner.run(inv)]
    final = events[-1]
    assert final["payload"]["kind"] == "repeated_failure"
    fs = final["payload"]["detail"]["full_summary"]
    assert fs.startswith("failed read_file: ")   # colon proves preview populated
    assert len(fs) > len("failed read_file: ")


async def test_anthropic_interactive_never_trips_loop(tmp_path) -> None:
    (tmp_path / "art").mkdir()
    # Many IDENTICAL prose turns would trip repeated_failure at K on a
    # non-interactive run (and no_progress at N). With interactive=True the
    # guard is disabled, so the run consumes every prose turn and terminates
    # cleanly on the trailing complete_step.
    n_prose = DEFAULT_NOPROGRESS_N + DEFAULT_REPEAT_K + 2
    responses = [response(text("still prose, no tool call"), stop_reason="end_turn")
                 for _ in range(n_prose)]
    responses.append(response(
        text("done"),
        tool_use("c1", "complete_step", {"status": "Success"})))
    fake = FakeAsyncAnthropic(responses)
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log),
                  interactive=True,
                  continuation_fn=lambda _last: ("keep going", False))
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert not any(e["type"] == "error" and e["payload"].get("category") == "loop"
                   for e in events)
