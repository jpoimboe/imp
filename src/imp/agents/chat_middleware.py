"""ChatMiddleware: auto-prompt the user between agent text turns.

Per review #4 + Spike 3: AgentMiddleware lives in
langchain.agents.middleware.types. Implements BOTH sync wrap_model_call
(primary; agent.invoke/stream are sync) AND async awrap_model_call
(for future astream use). Inspects response.result (the new AIMessage(s)
the LLM produced) and injects a HumanMessage into request.messages if
the last AIMessage had no tool_calls.

The CLI passes input_fn that reads from stdin (Plan 3 wraps prompt_toolkit
for ESC support). For tests we pass a deterministic input_fn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

try:
    from langchain.agents.middleware.types import (  # type: ignore[import-untyped]
        AgentMiddleware,
    )
except ImportError:  # pragma: no cover
    class AgentMiddleware:  # type: ignore[no-redef]
        pass


def _last_ai_message(response: Any) -> AIMessage | None:
    """Extract the most recent AIMessage from a ModelResponse's `result`."""
    result = getattr(response, "result", None)
    if isinstance(result, AIMessage):
        return result
    if isinstance(result, list):
        for msg in reversed(result):
            if isinstance(msg, AIMessage):
                return msg
    return None


class ChatMiddleware(AgentMiddleware):
    def __init__(self, input_fn: Callable[[], str]) -> None:
        self._input = input_fn

    def wrap_model_call(
        self, request: Any, handler: Callable[[Any], Any],
    ) -> Any:
        response = handler(request)
        ai = _last_ai_message(response)
        if ai is None or ai.tool_calls:
            return response
        user_text = self._input()
        request.messages.append(HumanMessage(content=user_text))
        return response

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Any],
    ) -> Any:
        response = await handler(request)
        ai = _last_ai_message(response)
        if ai is None or ai.tool_calls:
            return response
        user_text = await asyncio.to_thread(self._input)
        request.messages.append(HumanMessage(content=user_text))
        return response
