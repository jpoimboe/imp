"""task tool (spec §6.13) — async dispatch.

`task` is special: the runner doesn't call `.invoke()` on it. The runner's
dispatch loop detects `tool_use.name == "task"` and `await dispatch_task(...)`
directly — this is the only path that crosses the sync→async boundary, and
making it explicit avoids the run_coroutine_threadsafe-from-loop-thread
deadlock that would otherwise hit.
"""

from __future__ import annotations

import json
from typing import Any

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import JsonlEventLog

TASK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subagent_name": {"type": "string", "description": "Name of a declared subagent."},
        "task_description": {"type": "string", "description": "What the subagent should do."},
    },
    "required": ["subagent_name", "task_description"],
}


class TaskMustBeAwaited(Exception):
    """Sentinel raised if the runner forgets the special-case dispatch."""


def make_task_tool(tiers: tuple[str, ...] = ()) -> Tool:
    def _invoke(ctx: Any, **kwargs: Any) -> str:
        raise TaskMustBeAwaited(
            "runner must special-case tool_use.name=='task' and call "
            "agents.subagents.dispatch_task; do not invoke as a regular tool"
        )

    # Off-path: reuse the shared constant unchanged so the no-tiers schema is
    # byte-for-byte identical to pre-feature behavior.
    schema = TASK_TOOL_SCHEMA
    if tiers:
        schema = {
            "type": "object",
            "properties": {
                **TASK_TOOL_SCHEMA["properties"],
                "model_tier": {
                    "type": "string",
                    "enum": list(tiers),
                    "description": (
                        "Capability tier for the subagent, listed cheapest->"
                        "strongest. Pick by subtask difficulty; omit to inherit "
                        "the caller's model."
                    ),
                },
            },
            # model_tier stays OPTIONAL: not added to required.
            "required": list(TASK_TOOL_SCHEMA["required"]),
        }

    return Tool(
        name="task",
        description=(
            "Dispatch a subtask to a named subagent. "
            "Returns the subagent's structured output as a JSON string."
        ),
        input_schema=schema,
        fn=_invoke,
    )


async def dispatch_task(
    ctx: ToolContext, subagent_name: str, task_description: str,
    *, tool_call_event_id: str = "", model_tier: str | None = None,
) -> str:
    if subagent_name not in ctx.subagent_defs:
        raise ValueError(
            f"subagent {subagent_name!r} not declared; available: "
            f"{sorted(ctx.subagent_defs)}"
        )
    if ctx.spawn_subagent is None:
        raise RuntimeError("task dispatched but no spawn_subagent hook installed")
    if ctx.parent_transcript_dir is None:
        raise RuntimeError("ToolContext.parent_transcript_dir not set")

    subagent_def = ctx.subagent_defs[subagent_name]
    invocation_id = ctx.next_subagent_invocation_id()

    # Read change_context from ctx instead of fabricating it.
    cc = ctx.change_context or ChangeContext(
        slug="", title="", project_root=ctx.project_root,
        # Degenerate path (no inherited change_context): mirror Change.workflow_dir
        # (= project_root/.imp/change/workflow) so a fabricated context still
        # points at the snapshot root.
        workflow_dir=ctx.project_root / ".imp" / "change" / "workflow",
    )

    # Carryover important #1: render the subagent's Jinja template, same way
    # executor._route_agent renders the parent's. The PromptContext reads the
    # live events.jsonl for this Change so {{ events.<step>.<field> }} works.
    # Prompt rendering uses a SEPARATE read-only JsonlEventLog, NOT
    # ctx.event_log. ctx.event_log is the shared WRITE instance that fans
    # subagent audit events out to the parent's on_event chain; passing it
    # here would conflate the write-fan-out log with a read-only snapshot
    # cursor. Keep them distinct.
    prompt_ctx = build_context(
        change_slug=cc.slug,
        change_title=f"Change {cc.slug}",  # subagents don't see parent title
        event_log=JsonlEventLog(ctx.events_path),
    )
    rendered_prompt = render_prompt(subagent_def.prompt_template_source, prompt_ctx)

    # Choose the subagent's provider/model. Precedence (design §5):
    #   1. runtime model_tier — only when truthy AND in the resolved map.
    #      The "inherit" signal is None: Anthropic omits the optional arg
    #      (.get -> None); OpenAI strict forces model_tier into `required` but
    #      nullable, so the model sends JSON null -> None to decline. ("" can't
    #      come from a strict enum; `model_tier or None` coalesces it defensively.)
    #   2. the subagent's static model_role: frontmatter (today's behavior).
    #   3. inherit the parent's provider + model.
    # Runtime tier OVERRIDES static frontmatter (§5): otherwise the parent's
    # choice would be dead whenever a subagent declares a role. NOTE this means a
    # tier deliberately bypasses any model_rules (differ_provider_from) constraint
    # on the subagent's frontmatter role.
    # AgentInvocation is frozen, so this is decided BEFORE construction.
    resolved = ctx.resolved_roles or {}
    tier = model_tier or None           # None (or defensively "") -> inherit
    if tier is not None and tier not in resolved:
        valid = ", ".join(ctx.model_tiers) or "(no tiers configured)"
        raise ValueError(
            f"model_tier {tier!r} is not a configured tier; valid tiers: {valid}"
        )

    mr = subagent_def.model_role
    if tier is not None:
        pm = resolved[tier]
        sub_provider, sub_model = pm.provider, pm.model
    elif mr is not None and mr in resolved:
        pm = resolved[mr]
        sub_provider, sub_model = pm.provider, pm.model
    else:
        sub_provider, sub_model = ctx.provider_name, ctx.model_name

    sub_inv = AgentInvocation(
        agent_def=subagent_def,
        provider_name=sub_provider,
        model_name=sub_model,
        system_prompt=rendered_prompt,
        initial_messages=(Message(
            role="user", content=(TextBlock(text=task_description),),
        ),),
        change_context=cc,
        interactive=False,
        transcript_dir=ctx.parent_transcript_dir / "subagents" / f"{subagent_name}-{invocation_id}",
        fs_permissions=ctx.fs_permissions,
        events_path=ctx.events_path,
        subagent_defs={},
        # Stable subagent identity + shared write log + cross-agent parent link
        agent_id=f"sub:{tool_call_event_id}",
        event_log=ctx.event_log,
        first_event_parent_id=tool_call_event_id if tool_call_event_id else None,
        resolved_roles=ctx.resolved_roles,
    )

    gen = await ctx.spawn_subagent(sub_inv)
    final: Any = None
    async for event in gen:
        if event["type"] == "agent_output":
            final = event["payload"]
        elif event["type"] == "error":
            raise RuntimeError(event["payload"].get("message", "subagent error"))
    if final is None:
        raise RuntimeError("subagent finished without agent_output")
    return json.dumps(final)
