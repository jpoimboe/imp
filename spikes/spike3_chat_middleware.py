"""Spike 3: ChatMiddleware via langchain.agents.middleware.types.

VERDICT (2026-05-25, langchain 1.3.1 / deepagents 0.6.3): PASS w/ caveat.

What works (Task 15 design unchanged):
- Import path correct: `from langchain.agents.middleware.types import AgentMiddleware`
- `ModelResponse` is the handler's return type; has `.result` (list of messages)
  and `.structured_response` attrs. NO `.messages`.
- Middleware sees the AIMessage(s) the model just produced via `response.result`,
  NOT via request.messages (which holds what's about to go IN to the model).
- Must implement BOTH `wrap_model_call` (sync) AND `awrap_model_call` (async);
  langgraph errors at runtime if only one is supplied when invoked via the
  matching path. Task 15's plan only specified the async variant — needs
  both, with the sync one as the primary (agent.invoke/stream are sync).

Behavioral caveat (Task 15 should document for dogfooding):
- The "AIMessage with no tool_calls" condition we hook on is RARE in
  deepagents flows because the agent uses built-in tools (write_todos)
  on essentially every turn, and the structured_response itself surfaces
  via a tool call. The middleware may never fire in normal execution.
- Implication: ChatMiddleware as-spec'd (auto-prompt between text turns)
  matches the spec but may not produce the back-and-forth UX in practice.
  Plan 3 dogfooding will reveal whether interactive agents need a different
  primitive (e.g., the built-in `ask_user` tool).
- For V0 we ship ChatMiddleware as planned; it fires when applicable; if
  it never fires for a given agent, that agent just runs autonomously
  which is the expected V0 fallback.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import Literal

from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel


class _Out(BaseModel):
    status: Literal["Success", "Error"]
    reply_count: int = 0


_USER_INPUTS: Iterator[str] = iter(["please give your final answer with reply_count=2"])


def _canned_input() -> str:
    try:
        text = next(_USER_INPUTS)
        print(f"\n[canned-user] {text}")
        return text
    except StopIteration:
        return "bye"


class ChatMiddleware(AgentMiddleware):
    """Inspect request.messages AFTER wrap_model_call's handler runs.

    Note: must implement BOTH sync (wrap_model_call) and async
    (awrap_model_call) — langgraph errors at runtime if only one is provided
    when the caller invokes via the matching sync/async path. Plan 2 Task 15
    needs the sync version because agent.invoke()/agent.stream() are sync.
    """

    def wrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        response = handler(request)
        print(f"\n[middleware] response type: {type(response).__name__}")
        result = getattr(response, "result", None)
        print(f"[middleware] response.result type: {type(result).__name__}")
        # response.result is the new message(s) the LLM produced
        ai_msg: AIMessage | None = None
        if isinstance(result, list) and result:
            tail = result[-1]
            if isinstance(tail, AIMessage):
                ai_msg = tail
        elif isinstance(result, AIMessage):
            ai_msg = result
        if ai_msg is None:
            print(f"[middleware] response.result not AIMessage: {result!r:.120}")
            return response
        print(f"[middleware] AIMessage text: {str(ai_msg.content)[:80]!r}, "
              f"tool_calls={len(ai_msg.tool_calls or [])}")
        if not ai_msg.tool_calls:
            user_text = _canned_input()
            request.messages.append(HumanMessage(user_text))
        return response

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        # Async variant — symmetry with sync. Plan 3 may need this for
        # streaming token-by-token to the terminal.
        response = await handler(request)
        msgs = getattr(request, "messages", None)
        if msgs:
            last = msgs[-1]
            if isinstance(last, AIMessage) and not last.tool_calls:
                user_text = _canned_input()
                request.messages.append(HumanMessage(user_text))
        return response


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY", file=sys.stderr)
        return 2

    agent = create_deep_agent(
        model=ChatAnthropic(model="claude-haiku-4-5-20251001"),
        tools=[],
        system_prompt=(
            "You are a chat agent. Reply with one short sentence to "
            "whatever the user says. When the user asks for your final "
            "answer, produce structured response with status='Success' "
            "and reply_count equal to the number of replies you've sent."
        ),
        response_format=_Out,
        middleware=[ChatMiddleware()],
    )

    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "Say hi."}]},
        )
    except Exception as e:
        print(f"FAIL: agent.invoke raised: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    sr = result.get("structured_response") if isinstance(result, dict) else None
    print(f"\nFinal structured_response: {sr!r}")
    return 0 if sr is not None else 1


if __name__ == "__main__":
    sys.exit(main())
