"""Render an OutputSchema into a 'call complete_step' instruction.

Single source of truth, consumed only by the executor: it (1) appends the
text to every agent's system prompt and (2) seeds the non-interactive nudge
closure. No provider imports this module, so there is no providers→engine
import edge.
"""

from __future__ import annotations

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
