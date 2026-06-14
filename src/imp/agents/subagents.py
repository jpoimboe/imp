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

from imp.agents.parallel import emit_parallelism_warning_once
from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import ChangeContext, Message, TextBlock
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.agent_run import (
    SubagentSuspended as SubagentSuspended,
)
from imp.engine.agent_run import (
    build_agent_invocation,
    drive_invocation,
)
from imp.engine.backoff import (
    HEADLESS_RETRY_CEILING_SECONDS as _HEADLESS_RETRY_CEILING_SECONDS,
)
from imp.engine.backoff import (
    interruptible_sleep as _interruptible_sleep,
)
from imp.engine.state import JsonlEventLog

MAX_SUBAGENT_DEPTH = 3

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



def raise_if_suspended(exc: BaseException) -> None:
    """The thinnest shared per-provider surface: each provider's `task`
    wrapper calls this as the first line of its generic `except`, so a
    SubagentSuspended unwinds (re-raised) instead of being swallowed into an
    is_error tool_result. dispatch_task raises SubagentSuspended on a
    subagent-origin pause; this keeps the three triplicated wrappers honest."""
    if isinstance(exc, SubagentSuspended):
        raise exc


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
                "tier": {
                    "type": "string",
                    "enum": list(tiers),
                    "description": (
                        "Capability tier for the subagent, listed cheapest->"
                        "strongest. Pick by subtask difficulty; omit to inherit "
                        "the caller's model."
                    ),
                },
            },
            # tier stays OPTIONAL: not added to required.
            "required": list(TASK_TOOL_SCHEMA["required"]),
        }

    return Tool(
        name="task",
        description=(
            "Dispatch a subtask to a named subagent. "
            "Returns the subagent's structured output as a JSON string. "
            "Independent subtasks issued as multiple task calls in ONE "
            "assistant message run concurrently; issue dependent subtasks "
            "sequentially across turns. The relative ordering of task "
            "calls and non-task tools issued in the same message is "
            "unspecified: do not place a non-task tool that depends on a "
            "task's effects (or vice versa) in the same message as task calls."
        ),
        input_schema=schema,
        fn=_invoke,
    )


async def dispatch_task(
    ctx: ToolContext, subagent_name: str, task_description: str,
    *, tool_call_event_id: str = "", tier: str | None = None,
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

    # Nesting bound (threaded, not just a field): the child is one level deeper.
    child_depth = ctx.subagent_depth + 1
    if child_depth > MAX_SUBAGENT_DEPTH:
        raise ValueError(
            f"subagent nesting depth {child_depth} exceeds "
            f"MAX_SUBAGENT_DEPTH={MAX_SUBAGENT_DEPTH}"
        )

    subagent_def = ctx.subagent_defs[subagent_name]
    invocation_id = ctx.next_subagent_invocation_id()

    cc = ctx.change_context or ChangeContext(
        id="", slug="", title="", project_root=ctx.project_root,
        # No change context → no change_dir, no snapshot; live dir is the only option.
        workflow_dir=ctx.project_root / "imp" / "workflows" / "default",
    )

    # Render the subagent's Jinja template. A SEPARATE read-only JsonlEventLog
    # for {{ events.* }} — NOT ctx.event_log, which is the shared WRITE instance.
    prompt_ctx = build_context(
        change_id=cc.id,
        change_slug=cc.slug,
        change_title=cc.title,   # dispatch label is the title now [item 11]
        event_log=JsonlEventLog(ctx.events_path),
        vars=ctx.change_state.vars if ctx.change_state is not None else {},
    )
    rendered_prompt = render_prompt(subagent_def.prompt_template_source, prompt_ctx)

    # Resolve (provider, model). Precedence (design §4):
    #   1. runtime tier (truthy AND in the resolved map) — OVERRIDES static.
    #   2. the subagent's static tier frontmatter.
    #   3. inherit the parent's provider + model.
    resolved = ctx.resolved_tiers or {}
    runtime_tier = tier or None
    if runtime_tier is not None and runtime_tier not in resolved:
        valid = ", ".join(ctx.tiers) or "(no tiers configured)"
        raise ValueError(
            f"tier {runtime_tier!r} is not a configured tier; valid tiers: {valid}"
        )
    subagent_tier = subagent_def.tier
    if runtime_tier is not None:
        pm = resolved[runtime_tier]
        sub_provider, sub_model = pm.provider, pm.model
    elif subagent_tier is not None and subagent_tier in resolved:
        pm = resolved[subagent_tier]
        sub_provider, sub_model = pm.provider, pm.model
    else:
        sub_provider, sub_model = ctx.provider_name, ctx.model_name

    # Nesting enabled: the child may dispatch the subagents IT declares (resolved
    # against the pool the parent saw). Empty for today's leaf subagents, so this
    # preserves no-nesting for the shipped workflow while enabling+guarding it.
    child_subagent_defs = {
        n: ctx.subagent_defs[n]
        for n in subagent_def.subagents
        if n in ctx.subagent_defs
    }

    sub_inv = build_agent_invocation(
        agent_def=subagent_def,
        provider_name=sub_provider,
        model_name=sub_model,
        rendered_system_prompt=rendered_prompt,
        initial_messages=(Message(
            role="user", content=(TextBlock(text=task_description),)),),
        change_context=cc,
        user_chat_input_fn=None,
        transcript_dir=ctx.parent_transcript_dir / "subagents"
        / f"{subagent_name}-{invocation_id}",
        fs_permissions=ctx.fs_permissions,
        events_path=ctx.events_path,
        subagent_defs=child_subagent_defs,
        tiers=ctx.tiers,
        # Decision #4: agent-level ceiling (workflow-default fallback not
        # reachable here; deferred YAGNI). Closes "uncapped" for any subagent
        # that declares a ceiling.
        max_tool_calls=subagent_def.max_tool_calls,
        agent_id=f"sub:{tool_call_event_id}",
        event_log=ctx.event_log,
        change_state=None,
        first_event_parent_id=tool_call_event_id or None,
        resolved_tiers=ctx.resolved_tiers,
        coordinator=ctx.coordinator,
        subagent_depth=child_depth,
        # Same object as event_log= above, but a distinct role: log= is the
        # nudge-event sink passed to build_continuation_fn; event_log= is the
        # invocation's stored audit log. For subagents both are ctx.event_log.
        log=ctx.event_log,
        verify_command=ctx.verify_command,
        non_interactive=ctx.non_interactive,
        sandbox_policy=ctx.sandbox_policy,
    )

    # Drive via the shared loop, with backoff (omission #4). Each backoff
    # iteration spawns a FRESH runner (runners are single-use). emit_events=False
    # keeps subagent terminal events out of the parent's stream (today's
    # behavior); the runner still writes its own audit events to the shared log.
    #
    # §10/§12 safety: the original `anyio.sleep(result.delay)` was a raw,
    # non-interruptible sleep. Two escape hatches are applied here, matching
    # _route_agent in executor.py:
    #   1. Interruptible sleep — each tick checks should_pause so Ctrl-C returns
    #      within one tick (100 ms default) instead of blocking for the full delay.
    #   2. Headless ceiling — when ctx.non_interactive is True and cumulative
    #      backoff would exceed _HEADLESS_RETRY_CEILING_SECONDS, stop retrying
    #      and raise SubagentSuspended (unwinds as a clean pause).

    # Width cap (design Decision 5): acquire THIS invocation's CapacityLimiter
    # around the whole drive loop — the single drift-proof site that bounds all
    # four providers once the batch helper fans tasks out. Lazily created,
    # per-invocation, never inherited (nested-pool deadlock guard). Today's
    # dispatch is sequential, so there is only ever one borrower; the limiter is
    # a no-op for behavior until the concurrency core lands. The startup clamp
    # warning fires here, once, on the first dispatch.
    limiter = ctx.get_parallel_limiter()
    emit_parallelism_warning_once(ctx.event_log, ctx.agent_id)
    async with limiter:
        backoff = 0
        cumulative_backoff = 0.0
        while True:
            runner = await ctx.spawn_subagent(sub_inv)
            result = await drive_invocation(
                sub_inv, runner, coordinator=ctx.coordinator,
                log=ctx.event_log, emit_events=False, prior_backoff_attempts=backoff)
            if result.delay is None:
                break
            # §10/§12 headless ceiling (checked BEFORE sleeping, matching executor).
            if (ctx.non_interactive
                    and cumulative_backoff + result.delay > _HEADLESS_RETRY_CEILING_SECONDS):
                raise SubagentSuspended("retry ceiling reached during subagent backoff")
            backoff += 1
            await _interruptible_sleep(result.delay, ctx.coordinator)
            cumulative_backoff += result.delay
            if ctx.coordinator is not None and ctx.coordinator.should_pause:
                raise SubagentSuspended("paused during subagent backoff")

        if result.paused:
            # Unwind to the executor; resume restarts the subagent subtree fresh.
            raise SubagentSuspended("subagent paused")
        if result.fatal or result.errored or result.output is None:
            # The parent adjudicates a flaky subagent (is_error tool_result) — it
            # must not kill the task-lead. error_policy backoff has already been
            # applied above, so transient errors retried before reaching here.
            cat = result.error.category if result.error else "unknown"
            kind = result.error.kind if result.error else None
            # Embed the kind (e.g. "stalled" from the B.2 watchdog) so the parent's
            # is_error tool_result content carries it — Part D derives the subagent's
            # terminal glyph from this marker. Without it, a stalled subagent reads
            # as a generic ✗ failed and the operator can't tell it timed out.
            # CONSUMER CONTRACT: narrative._close_subagent_fold scans this error
            # content for the literal "[<kind>]" marker (only on is_error closes).
            # Keep the bracketed format and kind vocabulary in sync with that scan.
            suffix = f" [{kind}]" if kind else ""
            raise RuntimeError(f"subagent {subagent_name!r} failed ({cat}){suffix}")
        return json.dumps(result.output)
