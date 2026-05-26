"""OpenAI provider + runner (spec §6.10). Direct openai.AsyncOpenAI.

This module is split into three layers, mirroring providers/anthropic.py:
  - Translation helpers (this file, top half): imp Message ↔ OpenAI shape.
  - OpenAIAgentRunner.run() (Task 9): the agent loop.
  - OpenAIProvider + registration (Task 15).

Translation surface:
  imp Message(role=assistant, content=[Text, ToolUse, ToolUse])
    ↔ one OpenAI assistant message with `content: str` + `tool_calls: list`.
  imp Message(role=user, content=[ToolResult, ToolResult])
    ↔ multiple OpenAI messages with `role: "tool"`, one per ToolResult.
"""

from __future__ import annotations

import json
from typing import Any

from imp.agents.runner import (
    AgentInvocation,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.subagents import make_task_tool
from imp.agents.tools import Tool, get_registry
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.providers._openai_schema import to_openai_strict

# Runner-side imports (AsyncGenerator, AsyncIterator, os, ResumeState,
# classify_resume, count_tool_uses, dispatch_task, StepComplete, ToolContext,
# Transcript, validate_agent_output, Provider, Event, AgentRunner) are added
# in Task 9 (runner class) and Task 15 (Provider registration).


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same shape as providers/anthropic.py::_build_tools — kept verbatim to
    preserve the "agent declares tools once, every runner sees the same set"
    invariant. Provider-specific shimming happens at the *schema* layer, not
    the *registry* layer."""
    reg = get_registry()
    out: dict[str, Tool] = {}
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "ask_user" and not invocation.interactive:
            continue
        if name == "task":
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if invocation.interactive and "ask_user" in invocation.agent_def.tools:
        out["ask_user"] = reg["ask_user"]
    if "task" in invocation.agent_def.tools:
        out["task"] = make_task_tool()
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
    return out


def _openai_tool_schema(t: Tool) -> dict[str, Any]:
    """Translate an imp Tool to OpenAI's function-call tool entry, with the
    strict-mode transform applied to the parameters."""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": to_openai_strict(t.input_schema),
            "strict": True,
        },
    }


def _imp_to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert an imp message list to OpenAI's flattened message list.

    Assistant messages with multiple tool_use blocks → ONE OpenAI assistant
    message carrying all tool_calls.

    User messages with multiple tool_result blocks → MULTIPLE OpenAI
    `role: "tool"` messages, one per ToolResultBlock.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            out.append({"role": "system", "content": _join_text(m.content)})
            continue
        if m.role == "assistant":
            text = _join_text(m.content)
            tool_calls = []
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append({
                        "id": b.id,
                        "type": "function",
                        "function": {
                            "name": b.name,
                            "arguments": json.dumps(b.input),
                        },
                    })
            entry: dict[str, Any] = {"role": "assistant"}
            # OpenAI accepts `content: null` only when `tool_calls` is
            # present; otherwise it 400s. Emit `""` when there's no text,
            # regardless of tool_calls presence — accepted in all cases and
            # avoids the latent bug a refusal-followed-by-continuation
            # scenario would otherwise trigger.
            entry["content"] = text if text else ""
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue
        # user
        tool_results = [b for b in m.content if isinstance(b, ToolResultBlock)]
        if tool_results:
            for tr in tool_results:
                content = tr.content
                if tr.is_error:
                    # OpenAI doesn't have an is_error flag on tool messages;
                    # embed an explicit prefix so the model sees it.
                    content = f"[error] {content}"
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_use_id,
                    "content": content,
                })
            continue
        # Plain user text (initial prompt, or interactive follow-up)
        out.append({"role": "user", "content": _join_text(m.content)})
    return out


def _join_text(content: tuple[Any, ...]) -> str:
    """Concatenate TextBlock.text values; ignore other block types."""
    return "".join(b.text for b in content if isinstance(b, TextBlock))


def _openai_message_to_imp(
    msg: Any,
) -> tuple[Message, list[str]]:
    """Convert an OpenAI assistant message (from response.choices[0].message)
    to an imp Message + warnings list. Mirrors anthropic._anthropic_to_message.

    Unknown shapes (e.g., msg.refusal — OpenAI's refusal field) become
    runner_warning entries, never raise. This matches deferred-scope's
    "unknown content block types ... translate to runner_warning, never raise"
    contract for the new runners."""
    blocks: list[Any] = []
    warnings: list[str] = []

    text = getattr(msg, "content", None)
    if text:
        blocks.append(TextBlock(text=text))

    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        tc_type = getattr(tc, "type", "function")
        if tc_type != "function":
            warnings.append(f"skipped tool_call type {tc_type!r}")
            continue
        fn = getattr(tc, "function", None)
        if fn is None:
            warnings.append("tool_call missing function block")
            continue
        try:
            args = json.loads(fn.arguments) if fn.arguments else {}
        except json.JSONDecodeError as e:
            warnings.append(f"tool_call {tc.id!r} arguments not JSON: {e}")
            args = {}
        blocks.append(ToolUseBlock(id=tc.id, name=fn.name, input=args))

    refusal = getattr(msg, "refusal", None)
    if refusal:
        warnings.append(f"skipped refusal: {refusal[:80]!r}")

    return Message(role="assistant", content=tuple(blocks)), warnings


def _serialize(msg: Message) -> dict[str, Any]:
    """Same shape as anthropic._serialize — used by `agent_message` events."""
    return {
        "role": msg.role,
        "content": [
            {"type": "text", "text": b.text} if isinstance(b, TextBlock)
            else {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
            if isinstance(b, ToolUseBlock)
            else {"type": "tool_result", "tool_use_id": b.tool_use_id,
                  "content": b.content, "is_error": b.is_error}
            for b in msg.content
        ],
    }


# The runner class is added in Task 9. Provider class added in Task 15.
