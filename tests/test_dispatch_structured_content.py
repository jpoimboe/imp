from __future__ import annotations

import json

import pytest

from imp.agents.dispatch import TurnState, dispatch_one_tool_use
from imp.agents.runner import ImagePart, TextPart, ToolUseBlock
from imp.engine.state import InMemoryEventLog


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _parts():
    return [
        TextPart(text="shot.png — 800×600 PNG"),
        ImagePart(media_type="image/png", data="aGVsbG8=", width=800,
                  height=600, blob="ab12.png", nbytes=1234),
    ]


def _tool_returning(value):
    class _Tool:
        read_only = True

        async def async_invoke(self, ctx, args):
            return value
    return _Tool()


async def _dispatch(monkeypatch, tool, tu_id="t1"):
    import imp.agents.dispatch as d

    async def _noop(_ctx):
        return None
    monkeypatch.setattr(d._bg, "_sweep_bg", _noop)

    log = InMemoryEventLog()
    results: list = []
    await dispatch_one_tool_use(
        ctx=None, elog=log, aid="step:p/s", invocation=None,
        tools={"read_file": tool},
        tu=ToolUseBlock(id=tu_id, name="read_file", input={"path": "shot.png"}),
        tool_results=results, state=TurnState(tool_calls_used=0), ceiling=None)
    return results


@pytest.mark.anyio
async def test_list_content_part_result_threads_through_unflattened(monkeypatch) -> None:
    parts = _parts()
    results = await _dispatch(monkeypatch, _tool_returning(parts))
    assert len(results) == 1
    block = results[0]
    # Not JSON-flattened to a string — the list of parts flowed through intact.
    assert isinstance(block.content, list)
    assert block.content == parts
    assert block.content[1].media_type == "image/png"
    assert block.is_error is False


@pytest.mark.anyio
async def test_dict_result_still_flattens_to_json_string(monkeypatch) -> None:
    results = await _dispatch(monkeypatch, _tool_returning({"a": 1}))
    assert results[0].content == json.dumps({"a": 1}, default=str)


@pytest.mark.anyio
async def test_empty_list_is_not_structured_content(monkeypatch) -> None:
    results = await _dispatch(monkeypatch, _tool_returning([]))
    assert results[0].content == "[]"


@pytest.mark.anyio
async def test_mixed_list_is_not_structured_content(monkeypatch) -> None:
    value = [TextPart(text="x"), "surprise"]
    results = await _dispatch(monkeypatch, _tool_returning(value))
    # One non-part element -> the guard's all(...) fails -> JSON-flatten path.
    assert isinstance(results[0].content, str)
    assert results[0].content == json.dumps(value, default=str)
