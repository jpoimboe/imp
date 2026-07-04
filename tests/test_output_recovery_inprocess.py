"""Deterministic in-session fold-recovery for the in-process anthropic runner.

A complete_step call whose decoder folded a required field into a preceding
string field is recovered IN THE SAME turn: no nudge, no re-emit, exactly one
output_recovered event, and agent_output carries the repaired payload. The
in-process anthropic runner routes complete_step through dispatch.py's Ok branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers.anthropic import AnthropicAgentRunner
from tests._events import control_events
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use
from tests.test_anthropic_runner import _inv as _anthropic_inv

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_anthropic_folded_field_recovered_in_one_turn(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    # Required enum `status` folded into the tail of the optional string `note`.
    folded = 'did the work</note>\n<parameter name="status">Success</parameter>'
    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"note": folded})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    events = [e async for e in runner.run(_anthropic_inv(tmp_path, event_log=log))]

    ctrl = control_events(events)
    assert [e["type"] for e in ctrl] == ["agent_output"]
    assert ctrl[-1]["payload"] == {"note": "did the work", "status": "Success"}
    log_types = [e["type"] for e in log.read()]
    assert log_types.count("output_recovered") == 1
    assert log_types.count("output_validation_nudge") == 0
    assert "error" not in log_types
    rec = next(e for e in log.read() if e["type"] == "output_recovered")
    assert rec["payload"] == {"container": "note", "fields": ["status"]}


async def test_anthropic_clean_call_emits_no_recovery_event(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step",
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
    assert "output_recovered" not in log_types
    assert "error" not in log_types
