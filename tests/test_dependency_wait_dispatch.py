from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.agent_loop import run_agent_loop
from imp.agents.dispatch import TurnState, dispatch_turn_tool_uses
from imp.agents.loop_guard import LoopGuard
from imp.agents.loop_items import DependencyWaited, EndOfTurn
from imp.agents.runner import ToolUseBlock
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import InMemoryEventLog
from imp.providers.observe_session import ObserveSession


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(project_root=tmp_path,
                       events_path=tmp_path / "events.jsonl")


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


@pytest.mark.anyio
async def test_push_path_returns_terminal_event_after_the_tool_result(
    tmp_path: Path,
) -> None:
    ran: list[str] = []

    def _go_live(ctx: Any, **_kw: Any) -> str:
        ctx.dependency_wait = ("jibu68",)
        return "waiting for jibu68 to merge"

    def _noop(ctx: Any, **_kw: Any) -> str:
        ran.append("noop")
        return json.dumps({"ran": True})

    tools = {
        "go_live": Tool(name="go_live", description="",
                        input_schema=_schema(), fn=_go_live),
        "noop": Tool(name="noop", description="",
                     input_schema=_schema(), fn=_noop),
    }
    ctx = _ctx(tmp_path)
    elog = InMemoryEventLog()
    results: list[Any] = []
    events: list[dict[str, Any]] = []

    async for ev in dispatch_turn_tool_uses(
            ctx=ctx, elog=elog, aid="step:p/s", invocation=None, tools=tools,
            tool_uses=[ToolUseBlock(id="t1", name="go_live", input={}),
                       ToolUseBlock(id="t2", name="noop", input={})],
            tool_results=results, state=TurnState(tool_calls_used=0),
            ceiling=None):
        events.append(ev)

    assert events == [{"type": "dependency_wait",
                       "payload": {"depends_on": ["jibu68"]}}]
    # The tool_result was appended BEFORE the terminal event (transcript invariant)…
    assert [b.tool_use_id for b in results] == ["t1"]
    # …and the tool queued behind it in the same batch never executed.
    assert ran == []


@pytest.mark.anyio
async def test_push_path_returns_none_without_the_flag(tmp_path: Path) -> None:
    tools = {"noop": Tool(name="noop", description="", input_schema=_schema(),
                          fn=lambda ctx, **kw: json.dumps({"ran": True}))}
    events: list[dict[str, Any]] = []
    results: list[Any] = []

    async for ev in dispatch_turn_tool_uses(
            ctx=_ctx(tmp_path), elog=InMemoryEventLog(), aid="step:p/s",
            invocation=None, tools=tools,
            tool_uses=[ToolUseBlock(id="t1", name="noop", input={})],
            tool_results=results, state=TurnState(tool_calls_used=0),
            ceiling=None):
        events.append(ev)

    assert [e["type"] for e in events] == ["tool_heartbeat"]
    assert [b.tool_use_id for b in results] == ["t1"]


class _FakeObserve(ObserveSession):
    """Minimal observe session: one raw message, no items, terminal on demand."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(None, None)
        self.ctx = ctx
        self.guard = LoopGuard()
        self.terminal_checks = 0

    async def _receive(self) -> Any:
        yield {"raw": 1}

    async def _message_to_items(self, raw: Any) -> Any:
        return
        yield  # pragma: no cover — makes this an async generator

    def _fatal_error(self) -> dict[str, Any] | None:
        return None

    async def _is_terminal(self, raw: Any) -> bool:
        self.terminal_checks += 1
        return True

    def _flush_turn_record(self) -> Any:
        return None

    async def _terminal_error(self, last_text: str) -> dict[str, Any] | None:
        return None

    def _last_text(self) -> str:
        return "done"

    def _trailing_text(self) -> str:
        return "done"


@pytest.mark.anyio
async def test_observe_path_yields_before_the_terminal_check(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    ctx.dependency_wait = ("jibu68",)
    session = _FakeObserve(ctx)

    items = [item async for item in session.events()]

    assert items == [DependencyWaited(deps=("jibu68",))]
    # _is_terminal is where a captured complete_step becomes the step's output;
    # never reaching it is what keeps a stray same-turn complete_step out.
    assert session.terminal_checks == 0


@pytest.mark.anyio
async def test_observe_path_falls_through_without_the_flag(
    tmp_path: Path,
) -> None:
    session = _FakeObserve(_ctx(tmp_path))

    items = [item async for item in session.events()]

    assert items == [EndOfTurn(last_text="done")]
    assert session.terminal_checks == 1


class _FakeSession:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.ctx = None
        self.elog = InMemoryEventLog()
        self.aid = "step:p/s"
        self.guard = LoopGuard()
        self.ceiling = None
        self.tool_calls_used = 0
        self.subagent_depth = 0
        self.tools: dict[str, Any] = {}
        self.opened = False

    async def open(self) -> None:
        self.opened = True

    def cancelled(self) -> bool:
        return False

    async def events(self) -> Any:
        for item in self._items:
            yield item


@pytest.mark.anyio
async def test_loop_maps_dependency_waited_to_the_terminal_event() -> None:
    session = _FakeSession([DependencyWaited(deps=("jibu68", "koze82"))])

    out = [ev async for ev in run_agent_loop(None, session)]

    assert out == [{"type": "dependency_wait",
                    "payload": {"depends_on": ["jibu68", "koze82"]}}]
