"""Render an OutputSchema into a 'call complete_step' instruction.

Single source of truth, consumed by the shared `engine.agent_run` builder
(and through it, both the executor and dispatch_task). No provider imports
this module, so there is no providers→engine import edge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from imp.engine.types import OutputSchema


def completion_instruction(schema: OutputSchema) -> str:
    """Prose instruction naming every field the agent must pass to
    `complete_step`, with enum values inlined and optional fields marked."""
    assert schema.fields, "OutputSchema must have at least one field"
    parts: list[str] = []
    for field in schema.fields:
        if field.type == "enum":
            spec = f"`{field.name}` ({'|'.join(field.values)})"
        else:
            spec = f"`{field.name}` ({field.type})"
        if not field.required:
            spec += " (optional)"
        parts.append(spec)
    fields_text = ", ".join(parts)
    return (
        "To finish this step you MUST call the `complete_step` tool with: "
        f"{fields_text}. Do not answer in prose — call the tool."
    )


NUDGE_LIMIT = 2


def build_continuation_fn(
    *,
    interactive: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    instruction: str,
    log: Any,
    agent_id: str | None,
) -> Callable[[str], str | None | Awaitable[str | None]] | None:
    """Build the end-of-turn continuation hook the provider calls when an
    agent ends a turn without complete_step.

    interactive: wrap the user chat input — the human keeps the conversation
    going, so it always returns a string (an interactive agent ends only by
    calling complete_step). The closure emits `awaiting_user_input` BEFORE it
    reads input, mirroring `human_prompt_pending`, so out-of-process drivers
    know input is awaited (provider-native stop_reason can't carry this).

    non-interactive: a fresh nudge counter per invocation. The first
    NUDGE_LIMIT calls return the corrective instruction; after that, None —
    which makes the provider emit agent_incomplete (-> Pause). This is the
    backstop subagents never had (omission #2 / the dogfood bug).
    """
    if interactive:
        if user_chat_input_fn is None:
            return None

        def _interactive(last_text: str) -> str | Awaitable[str]:
            if log is not None:
                log.append(agent_id=agent_id, type="awaiting_user_input",
                           payload={"prompt": last_text})
            return user_chat_input_fn("")

        return _interactive

    nudge = f"You ended your turn without calling `complete_step`. {instruction}"
    count = 0

    def _nudge(_last_text: str) -> str | None:
        nonlocal count
        if count >= NUDGE_LIMIT:
            return None
        count += 1
        return nudge

    return _nudge
