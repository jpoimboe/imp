"""ESC sentinel from prompt: ChatMiddleware re-prompts instead of submitting."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from imp.agents.chat_middleware import ChatMiddleware
from imp.cli.prompt import ESC_SENTINEL


@dataclass
class _Req:
    messages: list[Any] = field(default_factory=list)


@dataclass
class _Resp:
    result: Any = None


def test_esc_sentinel_reprompts() -> None:
    answers: Iterator[str] = iter([ESC_SENTINEL, "real reply"])
    mw = ChatMiddleware(input_fn=lambda: next(answers))
    req = _Req(messages=[HumanMessage("hi")])
    resp = _Resp(result=[AIMessage("hello back")])
    mw.wrap_model_call(req, lambda _r: resp)  # type: ignore[arg-type]
    assert req.messages[-1].content == "real reply"
