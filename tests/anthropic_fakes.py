# tests/anthropic_fakes.py
"""Fake anthropic SDK client for unit-testing AnthropicAgentRunner.

The real client exposes `messages.create(...)` returning a response with
.content list of typed blocks. The fake returns a sequence of canned
responses in order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FakeTextBlock:
    type: str
    text: str


@dataclass
class FakeToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeResponse:
    content: list[Any]
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(
        self, responses: list[FakeResponse],
        default: FakeResponse | None = None,
    ) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise IndexError("FakeMessages: out of canned responses (no default set)")


class FakeAsyncAnthropic:
    def __init__(
        self,
        responses: list[FakeResponse],
        *,
        default_response: FakeResponse | None = None,
    ) -> None:
        """`responses` are returned in order. Once exhausted, `default_response`
        (if set) is returned indefinitely. Use the default for e2e tests that
        don't want to count exact LLM-call counts."""
        self.messages = _FakeMessages(responses, default_response)


def text(s: str) -> FakeTextBlock:
    return FakeTextBlock(type="text", text=s)


def tool_use(id: str, name: str, input: dict[str, Any]) -> FakeToolUseBlock:
    return FakeToolUseBlock(type="tool_use", id=id, name=name, input=input)


def response(*blocks: Any, stop_reason: str = "tool_use") -> FakeResponse:
    return FakeResponse(content=list(blocks), stop_reason=stop_reason)


@dataclass
class FakeThinkingBlock:
    type: str  # "thinking"
    thinking: str


def thinking(text: str) -> FakeThinkingBlock:
    return FakeThinkingBlock(type="thinking", thinking=text)
