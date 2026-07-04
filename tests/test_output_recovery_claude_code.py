"""Deterministic in-session fold-recovery for the claude_code provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_use
from tests.test_claude_code_runner import _inv

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_folded_field_recovered_without_requery(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    # Required enum `status` folded into the tail of the optional string `note`.
    folded = 'did the work</note>\n<parameter name="status">Success</parameter>'
    scripted = [
        assistant(tool_use("c1", "complete_step", {"note": folded})),
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

    assert [e["type"] for e in events] == ["agent_output"], events
    assert events[-1]["payload"] == {"note": "did the work", "status": "Success"}
    log_types = [e["type"] for e in log.read()]
    assert log_types.count("output_recovered") == 1
    assert log_types.count("output_validation_nudge") == 0
    # Deterministic recovery: no corrective nudge was re-queried into the session.
    assert not any("complete_step call was invalid" in str(q)
                   for q in captured["c"].queries)
    rec = next(e for e in log.read() if e["type"] == "output_recovered")
    assert rec["payload"] == {"container": "note", "fields": ["status"]}
