"""load_skill (spec §3) — auto-injected built-in, like complete_step. NOT a
registry @imp_tool: built per-step by a factory and injected unconditionally in
each provider's _build_tools, so any agent can call it. It advertises no skill
names (the agent's own prompt names the skill). Reads
<change.workflow_dir>/skills/agent/<name>.md, falling back to
skills/user/<name>.md, and emits a skill_load event."""

from __future__ import annotations

from typing import Any

from imp.agents.skills import load_agent_skill
from imp.agents.tools import Tool

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "name of the skill to load"},
    },
    "required": ["name"],
}


def make_load_skill_tool() -> Tool:
    def _invoke(ctx: Any, name: str, **_ignore: Any) -> str:
        # **_ignore tolerates any extra model-emitted arg (mirrors complete_step's
        # _invoke(ctx, **kwargs)); a stray arg must not become a TypeError.
        skills_dir = ctx.quest_context.workflow_dir / "skills"
        body = load_agent_skill(skills_dir, name)  # raises -> guarded is_error result
        if ctx.event_log is not None:
            ctx.event_log.append(
                agent_id=None, type="skill_load",
                payload={"name": name, "source": "agent"},
            )
        return body

    return Tool(
        name="load_skill",
        description=(
            "Load a named skill and return its instructions. Use this when your "
            "prompt tells you a specific skill is available for the task."
        ),
        input_schema=_INPUT_SCHEMA,
        fn=_invoke,
    )
