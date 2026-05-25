"""ChatMiddleware auto-prompts user between agent text turns.

Tests cover BOTH sync (wrap_model_call) and async (awrap_model_call)
paths and verify ModelResponse.result shape per Spike 3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from imp.agents.chat_middleware import ChatMiddleware


@dataclass
class _FakeRequest:
    messages: list[Any] = field(default_factory=list)


@dataclass
class _FakeResponse:
    """Stand-in for langchain's ModelResponse. Real type has `result` and
    `structured_response`; middleware only reads `result`."""
    result: Any = None


def test_sync_text_only_response_triggers_prompt() -> None:
    typed: Iterator[str] = iter(["my reply"])
    mw = ChatMiddleware(input_fn=lambda: next(typed))
    req = _FakeRequest(messages=[HumanMessage("hi")])
    resp = _FakeResponse(result=[AIMessage("hello back")])

    def _handler(r: _FakeRequest) -> _FakeResponse:
        return resp

    out = mw.wrap_model_call(req, _handler)  # type: ignore[arg-type]
    assert out is resp
    assert isinstance(req.messages[-1], HumanMessage)
    assert req.messages[-1].content == "my reply"


def test_sync_tool_call_response_does_not_prompt() -> None:
    called: list[int] = []
    mw = ChatMiddleware(input_fn=lambda: (called.append(1), "x")[1])
    req = _FakeRequest(messages=[HumanMessage("hi")])
    resp = _FakeResponse(result=[
        AIMessage(content="invoking", tool_calls=[
            {"id": "1", "name": "x", "args": {}},
        ]),
    ])

    def _handler(r: _FakeRequest) -> _FakeResponse:
        return resp

    mw.wrap_model_call(req, _handler)  # type: ignore[arg-type]
    assert called == []
    assert len(req.messages) == 1


def test_async_text_only_response_triggers_prompt() -> None:
    typed: Iterator[str] = iter(["async reply"])
    mw = ChatMiddleware(input_fn=lambda: next(typed))
    req = _FakeRequest(messages=[HumanMessage("hi")])
    resp = _FakeResponse(result=[AIMessage("async hello")])

    async def _handler(r: _FakeRequest) -> _FakeResponse:
        return resp

    out = asyncio.run(mw.awrap_model_call(req, _handler))  # type: ignore[arg-type]
    assert out is resp
    assert req.messages[-1].content == "async reply"


def test_result_can_be_single_ai_message() -> None:
    """response.result is sometimes a single AIMessage (not a list)."""
    typed: Iterator[str] = iter(["ok"])
    mw = ChatMiddleware(input_fn=lambda: next(typed))
    req = _FakeRequest(messages=[HumanMessage("hi")])
    resp = _FakeResponse(result=AIMessage("solo"))  # single, not list

    def _handler(r: _FakeRequest) -> _FakeResponse:
        return resp

    mw.wrap_model_call(req, _handler)  # type: ignore[arg-type]
    assert req.messages[-1].content == "ok"
