"""Spike 5: how do we get per-token chunks from a deepagents stream?

NOT RUN AUTOMATICALLY — requires ANTHROPIC_API_KEY. Run interactively:
  ANTHROPIC_API_KEY=sk-... python spikes/spike5_streaming.py

Goal: capture (a) token-level deltas as they arrive, AND (b) the final
structured_response. Probe three options:
  1. agent.stream(stream_mode="messages")
  2. agent.stream(stream_mode=["updates", "messages"])
  3. agent.astream_events(version="v2")

Pick whichever gives both clean tokens AND structured_response.

UPDATE THIS COMMENT BLOCK after running with the verdict:
  WINNER: <option N>
  TOKEN SHAPE: <chunk type / how to extract .content>
  STRUCTURED RESPONSE LOCATION: <key path>
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Literal

from deepagents import create_deep_agent
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel


class _Out(BaseModel):
    status: Literal["Success"]
    message: str = ""


def _agent():
    return create_deep_agent(
        model=ChatAnthropic(model="claude-haiku-4-5-20251001"),
        tools=[],
        system_prompt=(
            "You are a test agent. Write 'streaming works' across three short "
            "sentences, then produce status='Success' as the structured response."
        ),
        response_format=_Out,
    )


def probe_messages_mode() -> None:
    print("\n=== Option 1: stream_mode='messages' ===")
    agent = _agent()
    final = None
    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": "go"}]},
        stream_mode="messages",
    ):
        # In messages mode, chunk is a (message_chunk, metadata) tuple
        print(f"chunk type: {type(chunk).__name__} repr: {chunk!r}"[:200])
        final = chunk
    print(f"final: {final!r}"[:200])


def probe_dual_mode() -> None:
    print("\n=== Option 2: stream_mode=['updates','messages'] ===")
    agent = _agent()
    structured = None
    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": "go"}]},
        stream_mode=["updates", "messages"],
    ):
        # In multi-mode, chunk is a (mode, payload) tuple
        print(f"chunk: {chunk!r}"[:200])
        if isinstance(chunk, tuple) and chunk[0] == "updates":
            for v in chunk[1].values() if isinstance(chunk[1], dict) else []:
                if isinstance(v, dict) and "structured_response" in v:
                    structured = v["structured_response"]
    print(f"structured: {structured!r}")


def probe_astream_events() -> None:
    print("\n=== Option 3: astream_events ===")

    async def _run():
        agent = _agent()
        structured = None
        async for ev in agent.astream_events(
            {"messages": [{"role": "user", "content": "go"}]},
            version="v2",
        ):
            kind = ev.get("event", "")
            name = ev.get("name", "")
            if kind in ("on_chat_model_stream", "on_chain_stream",
                        "on_chain_end"):
                data = ev.get("data", {})
                if "chunk" in data:
                    chunk = data["chunk"]
                    print(f"[{kind}/{name}] chunk: "
                          f"{type(chunk).__name__} content="
                          f"{getattr(chunk, 'content', '')!r}"[:150])
                if "output" in data and isinstance(data["output"], dict):
                    if "structured_response" in data["output"]:
                        structured = data["output"]["structured_response"]
        print(f"structured: {structured!r}")

    asyncio.run(_run())


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    probe_messages_mode()
    probe_dual_mode()
    probe_astream_events()
    return 0


if __name__ == "__main__":
    sys.exit(main())
