"""load_skill — auto-injected built-in, like complete_step. NOT a registry
@imp_tool: built per-step by a factory and injected unconditionally in each
provider's _build_tools, so any agent can call it. It advertises no skill names
(the agent's own prompt names the skill). Reads
<quest.workflow_dir>/skills/<name>.md, falling back to skills/user/<name>.md.
Skills resolved from the primary skills_dir are rendered through imp's prompt
engine (so {{ docs.path }} resolves); a body resolved via the skills/user/
fallback (project-authored) is returned raw. Emits a skill_load event."""

from __future__ import annotations

from typing import Any

from imp.agents.prompts import build_context, render_prompt
from imp.agents.skills import resolve_internal_skill
from imp.agents.tools import Tool
from imp.engine.state import InMemoryEventLog

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
        qc = ctx.quest_context
        skills_dir = qc.workflow_dir / "skills"
        # raises -> guarded is_error result
        body, from_user_fallback = resolve_internal_skill(skills_dir, name)
        if not from_user_fallback:
            log = ctx.event_log if ctx.event_log is not None else InMemoryEventLog()
            body = render_prompt(
                body,
                build_context(
                    quest_title=qc.title, event_log=log,
                    quest_id=qc.id, quest_home=qc.quest_home,
                    project_facts=qc.project_facts,
                ),
                workflow_dir=qc.workflow_dir,
            )  # PromptRenderError -> guarded is_error result
        if ctx.event_log is not None:
            ctx.event_log.append(
                agent_id=None, type="skill_load",
                payload={"name": name, "source": "internal"},
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
