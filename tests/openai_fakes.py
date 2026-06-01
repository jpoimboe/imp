"""Fake openai SDK client for unit-testing OpenAIAgentRunner.

The real client exposes `chat.completions.create(...)` returning a
ChatCompletion with `.choices[0].message` carrying `.content: str | None` and
`.tool_calls: list[ChatCompletionMessageToolCall] | None`. The fake returns a
sequence of canned responses; once exhausted, an optional default is returned
indefinitely (mirrors tests/anthropic_fakes.FakeAsyncAnthropic semantics).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeFunction:
    name: str
    arguments: str  # JSON string per OpenAI shape


@dataclass
class FakeToolCall:
    id: str
    type: str  # always "function" in V1
    function: FakeFunction


@dataclass
class FakeMessage:
    role: str
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None
    # OpenAI sometimes adds `refusal: str | None`; ignored for now (Task 11
    # exercises the warn-on-unknown path).
    refusal: str | None = None


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class FakeCompletion:
    choices: list[FakeChoice] = field(default_factory=list)
    id: str = "chatcmpl_fake"
    model: str = "gpt-4o-mini"


class _FakeCompletions:
    def __init__(
        self, responses: list[FakeCompletion],
        default: FakeCompletion | None = None,
    ) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise IndexError(
            "FakeAsyncOpenAI: out of canned responses (no default set)"
        )


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class FakeAsyncOpenAI:
    def __init__(
        self,
        responses: list[FakeCompletion],
        *,
        default_response: FakeCompletion | None = None,
    ) -> None:
        self.chat = _FakeChat(_FakeCompletions(responses, default_response))


# Convenience constructors for tests
def message(
    *,
    content: str | None = None,
    tool_calls: list[FakeToolCall] | None = None,
    refusal: str | None = None,
) -> FakeMessage:
    return FakeMessage(role="assistant", content=content,
                       tool_calls=tool_calls, refusal=refusal)


def tool_call(id_: str, name: str, args: dict[str, Any]) -> FakeToolCall:
    return FakeToolCall(
        id=id_, type="function",
        function=FakeFunction(name=name, arguments=json.dumps(args)),
    )


def completion(*messages: FakeMessage, finish_reason: str = "tool_calls") -> FakeCompletion:
    """Most fixtures use a single-choice completion."""
    return FakeCompletion(
        choices=[FakeChoice(message=m, finish_reason=finish_reason) for m in messages],
    )


# ── Responses API fakes (migration) ──────────────────────────────────────────

@dataclass
class _Dumpable:
    """Carries a pre-built model_dump(mode='json') dict, mirroring the SDK's
    pydantic objects (the runner reads .model_dump, not attributes)."""
    _dump: dict[str, Any]

    def model_dump(self, mode: str | None = None) -> dict[str, Any]:
        return self._dump


def resp_message(text: str, *, citations: list[dict[str, Any]] | None = None,
                 role: str = "assistant") -> dict[str, Any]:
    annotations = []
    for c in citations or []:
        annotations.append({
            "type": "url_citation", "url": c.get("url"),
            "title": c.get("title"), "start_index": 0, "end_index": 0})
    return {
        "type": "message", "role": role,
        "content": [{"type": "output_text", "text": text,
                     "annotations": annotations}],
    }


def resp_function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id,
            "name": name, "arguments": arguments}


def resp_web_search_call(item_id: str, *, query: str) -> dict[str, Any]:
    return {"type": "web_search_call", "id": item_id, "status": "completed",
            "action": {"type": "search", "query": query}}


def resp_response(*output_items: dict[str, Any],
                  usage: dict[str, Any] | None = None) -> _Dumpable:
    return _Dumpable({
        "output": list(output_items),
        "usage": usage or {"input_tokens": 1, "output_tokens": 1},
    })


class _FakeResponses:
    def __init__(self, responses: list[Any], default: Any = None) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise IndexError("FakeResponsesClient: out of canned responses")


class FakeResponsesClient:
    def __init__(self, responses: list[Any], *, default_response: Any = None) -> None:
        self.responses = _FakeResponses(responses, default_response)
