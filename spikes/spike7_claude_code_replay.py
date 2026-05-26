"""Spike 7 — verify that `claude -p` accepts assistant/user history via
stream-json input AND does NOT re-execute tool_use blocks in that history.

This is the load-bearing experiment for V1's Claude Code runner (V1 spec §6.11).
If claude re-executes replayed tool_use blocks, the unified-transcript resume
story falls apart — bash side effects would re-run, files would re-overwrite.

Test design:
  - Register an in-process MCP tool `record_word(word)` that appends to a
    counter list every time it's invoked.
  - Build a "history" message stream:
      [assistant: text + tool_use(record_word, word="history-only")]
      [user: tool_result for that tool_use]
      [user: NEW prompt: "What word did you just record? Reply with one word."]
  - Run claude -p with --no-session-persistence, --setting-sources=[],
    --strict-mcp-config, --allowed-tools=mcp__imp_test__*, the imp_test MCP
    server providing record_word, and --disallowed-tools listing the
    common built-ins (Bash/Read/Write/...) so claude can't escape.
  - Observe:
      * tool invocation counter at end of run
      * what claude said in its final response

Outcomes:
  - counter == 0 AND response mentions "history-only" → REPLAY WORKS
      claude treated the history as context only, generated a fresh assistant
      turn that referenced the prior result.
  - counter >= 1 → REPLAY UNSAFE: claude re-executed the tool_use it saw
      in history. Spec needs a different mechanism.
  - counter == 0 but response is wrong → ambiguous; replay parsed but didn't
      land as context. Inspect output stream.

Run:
  ANTHROPIC_API_KEY=... python spikes/spike7_claude_code_replay.py
  (or Claude Code subscription auth via `claude` itself — no env var needed)
"""

from __future__ import annotations

import anyio
import json
import sys
import uuid

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    StreamEvent,
    create_sdk_mcp_server,
    tool,
)


# Counter shared with the in-process tool.
INVOCATIONS: list[dict] = []


@tool("record_word", "Record a single word for later recall.", {"word": str})
async def record_word(args):
    INVOCATIONS.append(dict(args))
    return {
        "content": [
            {"type": "text", "text": f"recorded: {args['word']}"}
        ]
    }


# Built-in Claude Code tools we want disabled so claude can only use mcp__imp_test__*
DISALLOWED = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch",
    "NotebookRead", "NotebookEdit", "Task", "TodoWrite", "BashOutput",
    "KillShell", "SlashCommand", "ExitPlanMode",
]


async def history_stream():
    """Async iterable producing the replay history + final user prompt."""
    session_id = "spike7"
    tool_use_id = f"toolu_{uuid.uuid4().hex[:12]}"

    # 1. Assistant turn that "previously" used the tool.
    yield {
        "type": "assistant",
        "session_id": session_id,
        "parent_tool_use_id": None,
        "uuid": str(uuid.uuid4()),
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "content": [
                {"type": "text", "text": "I'll record the word now."},
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "mcp__imp_test__record_word",
                    "input": {"word": "history-only"},
                },
            ],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }

    # 2. User turn carrying the (synthetic) tool result.
    yield {
        "type": "user",
        "session_id": session_id,
        "parent_tool_use_id": None,
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "recorded: history-only",
                    "is_error": False,
                }
            ],
        },
    }

    # 3. New user prompt that triggers the next turn.
    yield {
        "type": "user",
        "session_id": session_id,
        "parent_tool_use_id": None,
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "user",
            "content": "What word did you just record? Reply with one word only — no tools, no extra text.",
        },
    }


async def main():
    mcp = create_sdk_mcp_server(
        name="imp_test",
        version="0.0.1",
        tools=[record_word],
    )

    opts = ClaudeAgentOptions(
        system_prompt=(
            "You are a test harness. Answer concisely. "
            "Do not call any tools unless explicitly necessary."
        ),
        mcp_servers={"imp_test": mcp},
        allowed_tools=["mcp__imp_test__record_word"],
        disallowed_tools=DISALLOWED,
        setting_sources=[],
        strict_mcp_config=True,
        extra_args={"no-session-persistence": None},
        model="claude-sonnet-4-5",
        max_turns=5,
    )

    print("=" * 60)
    print("SPIKE 7 — Claude Code replay semantics")
    print("=" * 60)
    print(f"Disallowed: {DISALLOWED}")
    print(f"Allowed: mcp__imp_test__record_word only")
    print(f"Extra args: no-session-persistence")
    print(f"Setting sources: [] (none)")
    print()

    final_text: list[str] = []
    async with ClaudeSDKClient(opts) as client:
        await client.query(history_stream())
        async for msg in client.receive_response():
            kind = type(msg).__name__
            print(f"<<< {kind}", end="")
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    btype = type(block).__name__
                    print(f"  [{btype}]", end="")
                    if btype == "TextBlock":
                        print(f" {block.text!r}", end="")
                        final_text.append(block.text)
                    elif btype == "ToolUseBlock":
                        print(f" name={block.name!r} input={block.input!r}", end="")
            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    btype = type(block).__name__
                    print(f"  [{btype}]", end="")
                    if btype == "ToolResultBlock":
                        snippet = repr(block.content)[:80]
                        print(f" tool_use_id={block.tool_use_id!r} content={snippet}", end="")
            elif isinstance(msg, ResultMessage):
                print(f"  subtype={msg.subtype!r} duration_ms={msg.duration_ms}", end="")
                if msg.usage:
                    print(f"  in={msg.usage.get('input_tokens')} out={msg.usage.get('output_tokens')}", end="")
            print()

    print()
    print("=" * 60)
    print(f"Tool invocations during run: {len(INVOCATIONS)}")
    for i, inv in enumerate(INVOCATIONS):
        print(f"  [{i}] {inv}")
    print(f"Final assistant text: {' '.join(final_text)[:200]!r}")
    print()
    if len(INVOCATIONS) == 0:
        print("RESULT: REPLAY SAFE — claude did not re-execute the replayed tool_use.")
    else:
        print(f"RESULT: REPLAY UNSAFE — claude re-executed the tool {len(INVOCATIONS)} time(s).")
    print("=" * 60)


if __name__ == "__main__":
    anyio.run(main)
