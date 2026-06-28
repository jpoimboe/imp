"""Fake openai SDK client for unit-testing OpenAIAgentRunner.

The real client exposes `responses.stream(...)` returning an async context manager
whose `get_final_response()` returns a Response whose `.model_dump(mode="json")`
carries an `output[]` list of items (message / function_call / web_search_call / ...)
plus `usage`. The fake returns a sequence of canned responses; once exhausted, an
optional default is returned indefinitely (mirrors tests/anthropic_fakes.FakeAsyncAnthropic
semantics).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── Responses API fakes ──────────────────────────────────────────────────────

@dataclass
class _Dumpable:
    """Carries a pre-built model_dump(mode='json') dict, mirroring the SDK's
    pydantic objects (the runner reads .model_dump, not attributes)."""
    _dump: dict[str, Any]
    _request_id: str | None = None

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
                  usage: dict[str, Any] | None = None,
                  model: str | None = None) -> _Dumpable:
    dump: dict[str, Any] = {
        "output": list(output_items),
        "usage": usage or {"input_tokens": 1, "output_tokens": 1},
    }
    if model is not None:
        dump["model"] = model
    return _Dumpable(dump)


class _FakeHTTPResponse:
    def __init__(self, request_id: str | None) -> None:
        # Mirrors the openai stream's private `_response` (an httpx.Response): the
        # real production path reads the `x-request-id` header off it.
        self.headers = {"x-request-id": request_id} if request_id else {}


class _FakeRespStream:
    def __init__(self, final: Any, n_events: int) -> None:
        self._final = final
        self._events = [object() for _ in range(n_events)]
        self._response = _FakeHTTPResponse(getattr(final, "_request_id", None))

    async def __aenter__(self) -> _FakeRespStream:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        for ev in self._events:
            yield ev

    async def get_final_response(self) -> Any:
        return self._final


class _FakeResponses:
    def __init__(self, responses: list[Any], default: Any = None,
                 *, stream_events: int = 0) -> None:
        self._responses = list(responses)
        self._default = default
        self._stream_events = stream_events
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeRespStream:
        self.calls.append(kwargs)
        if self._responses:
            final: Any = self._responses.pop(0)
        elif callable(self._default):
            final = self._default(**kwargs)
        elif self._default is not None:
            final = self._default
        else:
            raise IndexError("FakeResponsesClient: out of canned responses")
        return _FakeRespStream(final, self._stream_events)


class FakeResponsesClient:
    def __init__(self, responses: list[Any], *, default_response: Any = None,
                 stream_events: int = 0) -> None:
        self.responses = _FakeResponses(
            responses, default_response, stream_events=stream_events)
