"""Spike 2: prove response_format= drives terminal output in deepagents.

VERDICT (2026-05-25, deepagents 0.6.3): PASS.

- result is a dict; structured response lives at result['structured_response']
  as a Pydantic instance of the response_format class.
- result keys: ['messages', 'structured_response', 'files']
- agent.invoke() terminates cleanly once the structured response is produced.

Streaming behavior (relevant for Task 17):
- Stream chunks are middleware-qualified maps. Observed keys:
  'PatchToolCallsMiddleware.before_agent', 'model', 'TodoListMiddleware.after_model'
- structured_response surfaces in the chunk under the 'model' key (the chunk
  emitted right after the LLM call that produced it).
- Task 17's chunk-flatten + structured-response-walk approach handles this.
- Note: deepagents emits an AIMessage + ToolMessage pair in that chunk even
  when no tools are called — the ToolMessage is part of how structured
  output is surfaced. Translation logic in Task 17 already handles ToolMessages.
"""

from __future__ import annotations

import os
import sys
from typing import Literal

from deepagents import create_deep_agent
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel


class StepOutput(BaseModel):
    status: Literal["Success", "Error"]
    message: str = ""


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY", file=sys.stderr)
        return 2

    agent = create_deep_agent(
        model=ChatAnthropic(model="claude-haiku-4-5-20251001"),
        tools=[],
        system_prompt=(
            "You are a test agent. Your only job is to produce a final "
            'structured response with status="Success" and '
            'message="hello world". Do not call any tools. Just return the '
            "structured response."
        ),
        response_format=StepOutput,
    )

    # Try .invoke first (simplest)
    result = agent.invoke({"messages": [{"role": "user", "content": "go"}]})

    print(f"result type: {type(result).__name__}")
    print(f"result keys: {list(result.keys()) if hasattr(result, 'keys') else 'no keys'}")
    # Common shapes: result["structured_response"], result["response"],
    # result.structured_response. We log all to find the right one.
    for k in ("structured_response", "response", "final_response"):
        if k in result:
            print(f"OK: result[{k!r}] = {result[k]!r}")
            break
    else:
        print(f"FAIL: no recognizable structured-output key in {list(result.keys())}",
              file=sys.stderr)
        print(f"full result: {result!r}", file=sys.stderr)
        return 1

    # Also confirm streaming returns the structured response somewhere
    print("\n--- streaming ---")
    chunk_count = 0
    saw_structured = False
    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": "go"}]},
        stream_mode="updates",
    ):
        chunk_count += 1
        keys = list(chunk.keys()) if hasattr(chunk, "keys") else "no keys"
        print(f"chunk {chunk_count} keys: {keys}")
        # Walk the chunk for structured_response
        if hasattr(chunk, "items"):
            for node_key, payload in chunk.items():
                if isinstance(payload, dict):
                    sr = payload.get("structured_response")
                    if sr is not None:
                        print(f"  structured_response under {node_key!r}: {sr!r}")
                        saw_structured = True
                    msgs = payload.get("messages")
                    if msgs:
                        print(f"  {len(msgs)} message(s) under {node_key!r}: "
                              f"{[type(m).__name__ for m in msgs]}")
    print(f"\nsaw_structured in stream: {saw_structured}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
