"""Fake openai SDK client for unit-testing OpenAIAgentRunner.

The real client exposes `responses.create(...)` returning a Response whose
`.model_dump(mode="json")` carries an `output[]` list of items (message /
function_call / web_search_call / ...) plus `usage`. The fake returns a
sequence of canned responses; once exhausted, an optional default is returned
indefinitely (mirrors tests/anthropic_fakes.FakeAsyncAnthropic semantics).
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


class _FakeResponses:
    def __init__(self, responses: list[Any], default: Any = None) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._responses:
            return self._responses.pop(0)
        if callable(self._default):
            return self._default(**kwargs)
        if self._default is not None:
            return self._default
        raise IndexError("FakeResponsesClient: out of canned responses")


class FakeResponsesClient:
    def __init__(self, responses: list[Any], *, default_response: Any = None) -> None:
        self.responses = _FakeResponses(responses, default_response)
