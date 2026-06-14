"""Render an OutputSchema into a 'call complete_step' instruction.

Single source of truth, consumed by the shared `engine.agent_run` builder
(and through it, both the executor and dispatch_task). No provider imports
this module, so there is no providers→engine import edge.
"""

from __future__ import annotations

from collections.abc import Callable

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
        f"{fields_text}. Structured arguments are not shown to the user, so "
        "state your conclusion in your turn text as prose; then call the tool."
    )


NUDGE_LIMIT = 2


def build_continuation_fn(*, instruction: str) -> Callable[[str], str | None]:
    """End-of-turn backstop, uniform across every agent and mode.

    Deliberate yielding is now the `wait_for_user` tool, so a bare turn-end is
    always an omission (the dogfood bug). Nudge up to NUDGE_LIMIT times, then
    return None — which makes the provider emit agent_incomplete (-> Pause). The
    nudge names BOTH terminators (R1) so a forgot-to-yield agent is not steered
    toward premature completion.
    """
    nudge = (
        "You ended your turn without calling `complete_step` (to finish the "
        "step) or `wait_for_user` (to yield to the human). " + instruction
    )
    count = 0

    def _nudge(_last_text: str) -> str | None:
        nonlocal count
        if count >= NUDGE_LIMIT:
            return None
        count += 1
        return nudge

    return _nudge
