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


# --- Bug I: visibility hint + _has_visible_text ---


def test_no_visible_text_emits_hint_before_prompt(capsys) -> None:
    """When the AIMessage had no user-visible text (e.g. thinking-only
    blocks), ChatMiddleware prints a hint so the user knows whose turn
    it is before being prompted."""
    from imp.agents.chat_middleware import _has_visible_text  # noqa: F401

    # Agent's AIMessage has only a thinking block — no user-visible text
    ai = AIMessage(content=[{"type": "thinking", "thinking": "..."}])
    req = _FakeRequest(messages=[HumanMessage("hi")])
    resp = _FakeResponse(result=[ai])

    answers = iter(["my reply"])
    mw = ChatMiddleware(input_fn=lambda: next(answers))
    mw.wrap_model_call(req, lambda _r: resp)  # type: ignore[arg-type]

    captured = capsys.readouterr().out
    assert "agent had no visible text" in captured


def test_visible_text_does_not_emit_hint(capsys) -> None:
    """If the AIMessage had visible text, no hint is printed (the renderer
    already showed the agent's text)."""
    ai = AIMessage(content="What do you want?")
    req = _FakeRequest(messages=[HumanMessage("hi")])
    resp = _FakeResponse(result=[ai])

    mw = ChatMiddleware(input_fn=lambda: "my reply")
    mw.wrap_model_call(req, lambda _r: resp)  # type: ignore[arg-type]

    captured = capsys.readouterr().out
    assert "agent had no visible text" not in captured


def test_has_visible_text_handles_block_shapes() -> None:
    from imp.agents.chat_middleware import _has_visible_text
    # Plain str
    assert _has_visible_text(AIMessage(content="hi"))
    assert not _has_visible_text(AIMessage(content=""))
    assert not _has_visible_text(AIMessage(content="   "))
    # Text block
    assert _has_visible_text(AIMessage(content=[{"type": "text", "text": "hi"}]))
    # Thinking-only
    assert not _has_visible_text(AIMessage(content=[{"type": "thinking", "thinking": "..."}]))
    # Mixed: thinking + text
    assert _has_visible_text(AIMessage(content=[
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "hi"},
    ]))
