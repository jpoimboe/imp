"""`Tool._bind` verified OFF the codex path, where it is the only normalizer.

On the codex path the arg model strips the null before `dispatch_tool` is even
entered, so a missing or wrong `_bind` would still pass every codex-path test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from imp.agents.dispatch import TurnState, dispatch_one_tool_use
from imp.agents.runner import ToolUseBlock
from imp.agents.tools import Tool
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.engine.state import InMemoryEventLog
from imp.engine.types import OutputField, OutputSchema, SourceLoc

_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "detail": {"type": "string"}},
    "required": ["path"],
}


def _tool(fn: Any) -> Tool:
    return Tool(name="probe", description="", input_schema=_SCHEMA, fn=fn)


def _sync_probe(observed: dict[str, Any]) -> Tool:
    def _fn(ctx: Any, path: str, detail: str = "signatures") -> str:
        observed["path"] = path
        observed["detail"] = detail
        return "ok"
    return _tool(_fn)


def _async_probe(observed: dict[str, Any]) -> Tool:
    async def _fn(ctx: Any, path: str, detail: str = "signatures") -> str:
        observed["path"] = path
        observed["detail"] = detail
        return "ok"
    return _tool(_fn)


def test_async_invoke_none_optional_reaches_body_as_declared_default() -> None:
    observed: dict[str, Any] = {}
    out = asyncio.run(
        _async_probe(observed).async_invoke(None, {"path": "m.py", "detail": None}))
    assert out == "ok"
    assert observed == {"path": "m.py", "detail": "signatures"}


def test_invoke_none_optional_reaches_body_as_declared_default() -> None:
    observed: dict[str, Any] = {}
    out = _sync_probe(observed).invoke(None, {"path": "m.py", "detail": None})
    assert out == "ok"
    assert observed == {"path": "m.py", "detail": "signatures"}


def test_none_required_arg_passes_through_untouched() -> None:
    observed: dict[str, Any] = {}
    out = asyncio.run(
        _async_probe(observed).async_invoke(None, {"path": None, "detail": "counts"}))
    assert out == "ok"
    assert observed == {"path": None, "detail": "counts"}


def _output_schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="spec_path", type="string", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def test_dispatch_complete_step_null_optional_passes_output_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anthropic/openai route (dispatch.py -> Tool.async_invoke): an explicit
    null for an optional output field is dropped before StepComplete, so output
    validation passes instead of rejecting 'expected string, got NoneType'."""
    import imp.agents.dispatch as d

    async def _noop(_ctx: Any) -> None:
        return None
    monkeypatch.setattr(d._bg, "_sweep_bg", _noop)  # type: ignore[attr-defined]

    schema = _output_schema()

    class _Inv:
        class agent_def:
            output_schema = schema

    terminal = asyncio.run(dispatch_one_tool_use(
        ctx=None, elog=InMemoryEventLog(), aid="step:p/s", invocation=_Inv(),
        tools={"complete_step": make_complete_step_tool(schema)},
        tu=ToolUseBlock(id="t1", name="complete_step",
                        input={"status": "Success", "spec_path": None}),
        tool_results=[], state=TurnState(tool_calls_used=0), ceiling=None))

    assert terminal == {"type": "agent_result", "payload": {"status": "Success"}}
