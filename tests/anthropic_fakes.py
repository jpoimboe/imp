# tests/anthropic_fakes.py
"""Fake anthropic SDK client for unit-testing AnthropicAgentRunner.

The real client exposes `messages.stream(...)` as an async context manager
that yields stream events. The fake returns a sequence of canned responses
in order via get_final_message().
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
    model: str | None = None
    _request_id: str | None = None
    usage: dict[str, Any] | None = None


class _FakeStream:
    def __init__(self, final: Any, n_events: int) -> None:
        self._final = final
        self._events = [object() for _ in range(n_events)]

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        for ev in self._events:
            yield ev

    async def get_final_message(self) -> Any:
        return self._final

    @property
    def request_id(self) -> str | None:
        # Mirrors anthropic's real AsyncMessageStream.request_id (which reads the
        # `request-id` response header): surface the canned FakeResponse's id so
        # the production stream.request_id extraction path is exercised.
        return getattr(self._final, "_request_id", None)


class _FakeMessages:
    def __init__(
        self, responses: list[FakeResponse],
        default: FakeResponse | None = None,
        *, stream_events: int = 0,
    ) -> None:
        self._responses = list(responses)
        self._default = default
        self._stream_events = stream_events
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        if self._responses:
            final: Any = self._responses.pop(0)
        elif callable(self._default):
            final = self._default(**kwargs)
        elif self._default is not None:
            final = self._default
        else:
            raise IndexError("FakeMessages: out of canned responses (no default set)")
        return _FakeStream(final, self._stream_events)


class FakeAsyncAnthropic:
    def __init__(
        self,
        responses: list[FakeResponse],
        *,
        default_response: FakeResponse | None = None,
        stream_events: int = 0,
    ) -> None:
        """`responses` are returned in order from get_final_message(). Once
        exhausted, `default_response` (if set) is returned indefinitely. Each
        stream() call yields `stream_events` sentinel events (default 0)."""
        self.messages = _FakeMessages(
            responses, default_response, stream_events=stream_events)


def text(s: str) -> FakeTextBlock:
    return FakeTextBlock(type="text", text=s)


def tool_use(id: str, name: str, input: dict[str, Any]) -> FakeToolUseBlock:
    return FakeToolUseBlock(type="tool_use", id=id, name=name, input=input)


def response(*blocks: Any, stop_reason: str = "tool_use",
             model: str | None = None,
             usage: dict[str, Any] | None = None) -> FakeResponse:
    return FakeResponse(content=list(blocks), stop_reason=stop_reason,
                        model=model, usage=usage)


@dataclass
class FakeThinkingBlock:
    type: str  # "thinking"
    thinking: str
    signature: str = ""


@dataclass
class FakeRedactedThinkingBlock:
    type: str  # "redacted_thinking"
    data: str


def thinking(text: str, signature: str = "") -> FakeThinkingBlock:
    return FakeThinkingBlock(type="thinking", thinking=text, signature=signature)


def redacted_thinking(data: str) -> FakeRedactedThinkingBlock:
    return FakeRedactedThinkingBlock(type="redacted_thinking", data=data)
