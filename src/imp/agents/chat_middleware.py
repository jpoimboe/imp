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
    from langchain.agents.middleware.types import AgentMiddleware
except ImportError:  # pragma: no cover
    class AgentMiddleware:  # type: ignore[no-redef]
        pass

try:
    from imp.cli.prompt import ESC_SENTINEL
except ImportError:  # pragma: no cover
    ESC_SENTINEL = "__IMP_ESC__"

from imp.cli.slash import is_slash_command, run_slash_command


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


def _has_visible_text(msg: AIMessage) -> bool:
    """Whether the agent's message had any plain-text content the user
    would have seen. Anthropic returns thinking blocks (`type: thinking`)
    and tool_use blocks; only `type: text` blocks are user-visible."""
    content = msg.content
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if str(block.get("text", "")).strip():
                    return True
            elif isinstance(block, str) and block.strip():
                return True
    return False


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
        if not _has_visible_text(ai):
            # User would otherwise stare at a bare `you>` with no context.
            print("[chat ] (agent had no visible text this turn — your turn)")
        while True:
            user_text = self._input()
            if user_text == ESC_SENTINEL:
                continue
            if is_slash_command(user_text):
                run_slash_command(user_text, output_fn=print)
                continue
            break
        request.messages.append(HumanMessage(content=user_text))
        return response

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Any],
    ) -> Any:
        response = await handler(request)
        ai = _last_ai_message(response)
        if ai is None or ai.tool_calls:
            return response
        if not _has_visible_text(ai):
            print("[chat ] (agent had no visible text this turn — your turn)")
        while True:
            user_text = await asyncio.to_thread(self._input)
            if user_text == ESC_SENTINEL:
                continue
            if is_slash_command(user_text):
                run_slash_command(user_text, output_fn=print)
                continue
            break
        request.messages.append(HumanMessage(content=user_text))
        return response
