"""Helpers for live elicitation tests (IMP_RUN_LIVE_ELICIT gated)."""
from __future__ import annotations

import os
from pathlib import Path

from imp.agents.runner import ContentBlock
from imp.agents.subagents import TASK_TOOL_SCHEMA, make_task_tool


def _anthropic_key() -> str | None:
    # Check key file before env so the conftest "sk-test" stub doesn't shadow it.
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _openai_key() -> str | None:
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("OPENAI_API_KEY") or None


async def collect_first_assistant_tool_uses(
    *,
    provider: str,
    prompt: str,
) -> list[ContentBlock]:
    """Make ONE real LLM call with the task tool; return the first assistant
    message's content blocks WITHOUT dispatching the tool calls."""
    task_tool = make_task_tool()
    if provider == "anthropic":
        return await _call_anthropic(prompt, task_tool.description)
    if provider == "openai":
        return await _call_openai(prompt, task_tool.description)
    raise ValueError(f"unsupported provider: {provider!r}")


async def _call_anthropic(prompt: str, tool_description: str) -> list[ContentBlock]:
    import anthropic

    from imp.providers.anthropic import (
        _parse_anthropic_message,
        _serialize_anthropic_response,
    )

    key = _anthropic_key()
    assert key is not None, (
        "anthropic credentials missing: set ANTHROPIC_API_KEY or create ~/.anthropic-key"
    )
    client = anthropic.AsyncAnthropic(api_key=key)
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You have two subagents named 'a' and 'b'. Use the task tool.",
            tools=[{
                "name": "task",
                "description": tool_description,
                "input_schema": TASK_TOOL_SCHEMA,
            }],
            messages=[{"role": "user", "content": prompt}],
            tool_choice={"type": "auto", "disable_parallel_tool_use": False},
        )
        raw = _serialize_anthropic_response(response)
        msg = _parse_anthropic_message(raw)
        return list(msg.content)
    finally:
        await client.close()


async def _call_openai(prompt: str, tool_description: str) -> list[ContentBlock]:
    import openai

    from imp.providers._openai_schema import to_openai_strict
    from imp.providers.openai import (
        _parse_responses_message,
        _serialize_responses_response,
    )

    key = _openai_key()
    assert key is not None, (
        "openai credentials missing: set OPENAI_API_KEY or create ~/.openai-key"
    )
    client = openai.AsyncOpenAI(api_key=key)
    try:
        response = await client.responses.create(
            model="gpt-4o-mini",
            instructions="You have two subagents named 'a' and 'b'. Use the task tool.",
            input=[{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            tools=[{
                "type": "function",
                "name": "task",
                "description": tool_description,
                "parameters": to_openai_strict(TASK_TOOL_SCHEMA),
                "strict": True,
            }],
            store=False,
            parallel_tool_calls=True,
        )
        raw = _serialize_responses_response(response)
        msg = _parse_responses_message(raw)
        return list(msg.content)
    finally:
        await client.close()
