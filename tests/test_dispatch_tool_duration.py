from __future__ import annotations

import pytest

from imp.agents.dispatch import TurnState, dispatch_one_tool_use
from imp.agents.runner import ToolUseBlock
from imp.engine.state import InMemoryEventLog


class _FakeTool:
    read_only = False

    async def async_invoke(self, ctx, args):
        return "ok-result"


class _FailTool:
    read_only = False

    async def async_invoke(self, ctx, args):
        raise RuntimeError("boom")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_in_process_tool_result_carries_duration(monkeypatch) -> None:
    import imp.agents.dispatch as d
    async def _noop(_ctx):  # _sweep_bg stub
        return None
    monkeypatch.setattr(d._bg, "_sweep_bg", _noop)

    log = InMemoryEventLog()
    results: list = []
    await dispatch_one_tool_use(
        ctx=None, elog=log, aid="step:p/s", invocation=None,
        tools={"read_file": _FakeTool()},
        tu=ToolUseBlock(id="t1", name="read_file", input={"p": 1}),
        tool_results=results, state=TurnState(tool_calls_used=0), ceiling=None)

    tr = next(e for e in log.read() if e["type"] == "tool_result")
    assert "duration_ms" in tr["payload"]
    assert tr["payload"]["duration_ms"] >= 0


@pytest.mark.anyio
async def test_in_process_tool_error_result_carries_duration(monkeypatch) -> None:
    import imp.agents.dispatch as d
    async def _noop(_ctx):
        return None
    monkeypatch.setattr(d._bg, "_sweep_bg", _noop)

    log = InMemoryEventLog()
    results: list = []
    await dispatch_one_tool_use(
        ctx=None, elog=log, aid="step:p/s", invocation=None,
        tools={"read_file": _FailTool()},
        tu=ToolUseBlock(id="t2", name="read_file", input={"p": 1}),
        tool_results=results, state=TurnState(tool_calls_used=0), ceiling=None)

    tr = next(e for e in log.read() if e["type"] == "tool_result")
    assert tr["payload"]["error"] is True
    assert "duration_ms" in tr["payload"]
    assert tr["payload"]["duration_ms"] >= 0
