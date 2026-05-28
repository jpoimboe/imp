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


def make_task_tool() -> Tool:
    def _invoke(ctx: Any, **kwargs: Any) -> str:
        raise TaskMustBeAwaited(
            "runner must special-case tool_use.name=='task' and call "
            "agents.subagents.dispatch_task; do not invoke as a regular tool"
        )

    return Tool(
        name="task",
        description=(
            "Dispatch a subtask to a named subagent. "
            "Returns the subagent's structured output as a JSON string."
        ),
        input_schema=TASK_TOOL_SCHEMA,
        fn=_invoke,
    )


async def dispatch_task(
    ctx: ToolContext, subagent_name: str, task_description: str,
    *, tool_call_event_id: str = "",
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
        change_id=0, project_root=ctx.project_root, artifacts_dir=ctx.artifacts_dir,
    )

    # Carryover important #1: render the subagent's Jinja template, same way
    # executor._execute_step renders the parent's. The PromptContext reads the
    # live events.jsonl for this Change so {{ events.<step>.<field> }} works.
    # Prompt rendering uses a SEPARATE read-only JsonlEventLog, NOT
    # ctx.event_log. ctx.event_log is the shared WRITE instance that fans
    # subagent audit events out to the parent's on_event chain; passing it
    # here would conflate the write-fan-out log with a read-only snapshot
    # cursor. Keep them distinct.
    prompt_ctx = build_context(
        change_id=cc.change_id,
        change_title=f"Change {cc.change_id}",  # subagents don't see parent title
        artifacts_dir=cc.artifacts_dir,
        event_log=JsonlEventLog(ctx.events_path),
    )
    rendered_prompt = render_prompt(subagent_def.prompt_template_source, prompt_ctx)

    sub_inv = AgentInvocation(
        agent_def=subagent_def,
        provider_name=ctx.provider_name,
        model_name=ctx.model_name,
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
