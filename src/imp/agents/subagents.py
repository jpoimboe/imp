"""task tool (spec §6.13) — async dispatch.

`task` is special: the runner doesn't call `.invoke()` on it. The runner's
dispatch loop detects `tool_use.name == "sub_agent"` and `await dispatch_task(...)`
directly — this is the only path that crosses the sync→async boundary, and
making it explicit avoids the run_coroutine_threadsafe-from-loop-thread
deadlock that would otherwise hit.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
from pathlib import Path
from typing import Any, cast

from imp.agents.parallel import emit_parallelism_warning_once
from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import ChangeContext, Message, TextBlock
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.agents.transcript import Transcript
from imp.engine.agent_run import (
    SubagentSuspended as SubagentSuspended,
)
from imp.engine.agent_run import (
    build_agent_invocation,
    drive_invocation,
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
            "runner must special-case tool_use.name=='sub_agent' and call "
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
        name="sub_agent",
        description=(
            "Dispatch a subtask to a named subagent. "
            "Returns the subagent's final message as a plain string. "
            "Independent subtasks issued as multiple sub_agent calls in ONE "
            "assistant message run concurrently; issue dependent subtasks "
            "sequentially across turns. The relative ordering of sub_agent "
            "calls and non-sub_agent tools issued in the same message is "
            "unspecified: do not place a non-sub_agent tool that depends on a "
            "sub_agent's effects (or vice versa) in the same message as sub_agent calls."
        ),
        input_schema=schema,
        fn=_invoke,
    )


_SUBAGENT_ID_RE = re.compile(r"-(\d+)$")


def _seed_subagent_counter(ctx: ToolContext) -> None:
    """Seed counter past pre-pause dirs so a resumed dispatch never reuses an existing dir."""
    if ctx._subagent_counter != 0:
        return
    assert ctx.parent_transcript_dir is not None
    d = ctx.parent_transcript_dir / "subagents"
    if not d.is_dir():
        return
    ids = [int(m.group(1)) for p in d.iterdir()
           if (m := _SUBAGENT_ID_RE.search(p.name))]
    ctx._subagent_counter = max(ids, default=0)


def _allocate_subagent_dir(ctx: ToolContext, subagent_name: str) -> tuple[int, Path]:
    """Allocate next invocation dir, bumping past dirs with non-empty transcripts."""
    _seed_subagent_counter(ctx)
    assert ctx.parent_transcript_dir is not None  # re-narrow (see helper above)
    base = ctx.parent_transcript_dir / "subagents"
    invocation_id = ctx.next_subagent_invocation_id()
    target = base / f"{subagent_name}-{invocation_id}"
    attempted_id = invocation_id
    collided: Path | None = None
    while Transcript(target / "transcript.jsonl").load():
        if collided is None:
            collided = target
        invocation_id = ctx.next_subagent_invocation_id()
        target = base / f"{subagent_name}-{invocation_id}"
    if collided is not None:
        _emit_collision_warning(ctx, collided, attempted_id, invocation_id)
    return invocation_id, target


def _emit_collision_warning(
    ctx: ToolContext, colliding: Path, attempted_id: int, reallocated_id: int,
) -> None:
    """Warn on dir re-allocation; message key is REQUIRED (UI renders payload["message"])."""
    if ctx.event_log is None:
        return
    with contextlib.suppress(Exception):
        ctx.event_log.append(
            agent_id=ctx.agent_id, type="runner_warning",
            payload={
                "message": (
                    f"subagent transcript dir collision: id {attempted_id} would "
                    f"resume the stale transcript at {colliding}; re-allocated to "
                    f"id {reallocated_id}"),
                "colliding_path": str(colliding),
                "attempted_id": attempted_id,
                "reallocated_id": reallocated_id,
            })


def _build_spike_policy(change: Any, worktree_path: Path, test_gates: Any) -> Any:
    """Build a distinct sandbox policy rooted at a throwaway spike worktree."""
    from imp.engine.env import engine_env, overlay_engine_env
    from imp.engine.sandbox import (
        resolve_policy,
        sandbox_available,
        sandbox_config_from_raw,
    )
    from imp.providers.effective import load_effective_config

    config = load_effective_config(
        project_root=change.project_root, control_root=change.control_root,
    ).config
    cfg = dataclasses.replace(
        sandbox_config_from_raw(config.sandbox or {}), network=True)
    eng = overlay_engine_env(
        os.environ,
        engine_env(project_root=worktree_path, quest_id=change.id,
                   change_title=change.title, quest_dir=change.quest_dir,
                   test_gates=test_gates),
    )
    return resolve_policy(
        worktree=worktree_path, control_root=change.control_root,
        quest_id=change.id, cfg=cfg, engine_env_result=eng,
        environ=os.environ, available=sandbox_available(),
        quest_dir=change.quest_dir, spike=True,
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
    invocation_id, target_dir = _allocate_subagent_dir(ctx, subagent_name)

    from imp.assets import resolve_workflow_dir
    from imp.providers.config import parse_default_workflow  # lazy: breaks import cycle

    isolated = subagent_def.isolated_worktree
    spike_worktree: Path | None = None
    if isolated:
        if ctx.change is None:
            raise RuntimeError(
                f"subagent {subagent_name!r} sets isolated_worktree but was "
                "dispatched with no live change context (nested dispatch); "
                "isolated_worktree is only supported from a top-level agent")
        from imp.engine import worktree as _worktree
        spike_worktree = _worktree.create_spike_worktree(
            ctx.change.control_root, ctx.change.id, invocation_id,
            ctx.change.base_branch)

    try:
        if isolated:
            from imp.engine.change import link_change_home
            assert spike_worktree is not None
            link_change_home(spike_worktree, ctx.change.quest_dir)
            dispatch_policy = _build_spike_policy(
                ctx.change, spike_worktree, ctx.test_gates)
            if not dispatch_policy.active and ctx.event_log is not None:
                ctx.event_log.append(
                    agent_id=ctx.agent_id, type="sandbox_inactive",
                    payload={"reason": dispatch_policy.note})
        else:
            dispatch_policy = ctx.sandbox_policy

        cc = ctx.change_context or ChangeContext(
            id="", title="", project_root=ctx.project_root,
            # No change context -> no quest_dir, no snapshot; live dir is the only option.
            workflow_dir=resolve_workflow_dir(
                ctx.project_root,
                parse_default_workflow(ctx.project_root / "imp" / "project.yaml"),
            ),
        )
        if isolated:
            cc = dataclasses.replace(cc, project_root=spike_worktree)

        # Render the subagent's Jinja template. A SEPARATE read-only JsonlEventLog
        # for {{ events.* }} — NOT ctx.event_log, which is the shared WRITE instance.
        prompt_ctx = build_context(
            quest_id=cc.id,
            change_title=cc.title,   # dispatch label is the title now [item 11]
            quest_home=cc.quest_home,
            event_log=JsonlEventLog(ctx.events_path),
            vars=ctx.change_state.vars if ctx.change_state is not None else {},
            project_facts=cc.project_facts,
        )
        rendered_prompt = render_prompt(
            subagent_def.prompt_template_source, prompt_ctx,
            workflow_dir=cc.workflow_dir)
        rendered_persona = render_prompt(
            cc.persona_source, prompt_ctx, workflow_dir=cc.workflow_dir,
        ) if cc.persona_source else ""

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
            chosen_tier: str | None = runtime_tier
            pm = resolved[runtime_tier]
            sub_provider, sub_model = pm.provider, pm.model
        elif subagent_tier is not None and subagent_tier in resolved:
            chosen_tier = subagent_tier
            pm = resolved[subagent_tier]
            sub_provider, sub_model = pm.provider, pm.model
        else:
            # Inherit the parent's provider/model with no explicit tier label.
            chosen_tier = None
            sub_provider, sub_model = ctx.provider_name, ctx.model_name

        # Nesting enabled: the child may dispatch the subagents IT declares (resolved
        # against the pool the parent saw). Empty for today's leaf subagents, so this
        # preserves no-nesting for the shipped workflow while enabling+guarding it.
        child_subagent_defs = {
            n: ctx.subagent_defs[n]
            for n in subagent_def.subagents
            if n in ctx.subagent_defs
        }

        def _build_sub_inv(target: Path, init_msgs: tuple[Message, ...]) -> Any:
            # Rebuilt verbatim per (re)dispatch so a reincarnation can re-seed the
            # initial message and re-target a fresh transcript dir; every other arg
            # is captured unchanged.
            return build_agent_invocation(
                agent_def=subagent_def,
                provider_name=sub_provider,
                model_name=sub_model,
                rendered_system_prompt=rendered_prompt,
                rendered_persona=rendered_persona,
                initial_messages=init_msgs,
                change_context=cc,
                user_chat_input_fn=None,
                transcript_dir=target,
                events_path=ctx.events_path,
                subagent_defs=child_subagent_defs,
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
                tier=chosen_tier,
                subagent_depth=child_depth,
                tiers=ctx.tiers,
                # Same object as event_log= above, but a distinct role: log= is the
                # event sink passed to build_continuation_fn (solicit/park events);
                # event_log= is the invocation's stored audit log. For subagents both
                # are ctx.event_log.
                log=ctx.event_log,
                test_gates=ctx.test_gates,
                headless=ctx.non_interactive,
                sandbox_policy=dispatch_policy,
                checkpoint_threshold=ctx.checkpoint_threshold,
            )

        sub_inv = _build_sub_inv(
            target_dir,
            (Message(role="user", content=(TextBlock(text=task_description),)),))

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
        #   2. Per-category budget — when cumulative backoff would exceed
        #      budget_for(category), stop retrying and raise SubagentSuspended
        #      (unwinds as a clean pause), identical in interactive and headless runs.

        # Width cap (design Decision 5): acquire THIS invocation's CapacityLimiter
        # around the whole drive loop — the single drift-proof site that bounds all
        # four providers once the batch helper fans tasks out. Lazily created,
        # per-invocation, never inherited (nested-pool deadlock guard). Today's
        # dispatch is sequential, so there is only ever one borrower; the limiter is
        # a no-op for behavior until the concurrency core lands. The startup clamp
        # warning fires here, once, on the first dispatch.

        # Lazy import: error_policy pulls providers/__init__, and this module is
        # in that cold-import chain — a top-level import would cycle.
        from imp.engine.error_policy import budget_for
        from imp.engine.reincarnation import reincarnation_seed, stash_transcript

        limiter = ctx.get_parallel_limiter()
        emit_parallelism_warning_once(ctx.event_log, ctx.agent_id)
        reincarnation_key = f"sub:{tool_call_event_id}"
        async with limiter:
            while True:  # reincarnation loop (Tier B backstop)
                backoff = 0
                cumulative_backoff = 0.0
                hint_exempt_sleeps = 0
                while True:
                    runner = await ctx.spawn_subagent(sub_inv)
                    result = await drive_invocation(
                        sub_inv, runner, coordinator=ctx.coordinator,
                        log=ctx.event_log, emit_events=False,
                        prior_backoff_attempts=backoff)
                    if result.delay is None:
                        break
                    # Per-category wall-clock budget (checked BEFORE sleeping,
                    # matching executor). category comes from the plumbed ErrorInfo,
                    # defaulting to "unknown" when absent. On exhaustion raise
                    # SubagentSuspended — this path's own clean-pause unwind (it
                    # cannot return an executor Pause).
                    category = result.error.category if result.error else "unknown"
                    # A hinted delay is an announced reset wait, not blind retrying:
                    # exempt it from the budget and don't accrue it, capped at 3 such
                    # sleeps per loop (the 4th is budgeted). Mirrors _route_agent.
                    hinted = result.delay_hinted and hint_exempt_sleeps < 3
                    if not hinted:
                        budget = budget_for(category)
                        if budget is not None and cumulative_backoff + result.delay > budget:
                            detail = ("re-authentication needed" if category == "auth"
                                      else "retry ceiling reached")
                            raise SubagentSuspended(
                                f"{detail} during subagent backoff (category={category})")
                    backoff += 1
                    # Surface the subagent backoff so a self-healed retry isn't silent.
                    if ctx.event_log is not None:
                        retry_payload = {
                            "category": category,
                            "attempt": backoff,
                            "backoff_seconds": result.delay,
                        }
                        if hinted:
                            retry_payload["hinted"] = True
                        ctx.event_log.append(
                            agent_id=sub_inv.agent_id, type="agent_retry",
                            payload=retry_payload)
                    await _interruptible_sleep(result.delay, ctx.coordinator)
                    if hinted:
                        hint_exempt_sleeps += 1
                    else:
                        cumulative_backoff += result.delay
                    if ctx.coordinator is not None and ctx.coordinator.should_pause:
                        raise SubagentSuspended("paused during subagent backoff")

                # Tier A proactive handoff: the subagent approached its window and
                # wrote a handoff note before stopping. Reborn INSIDE this dispatch
                # (the parent still sees one resolved sub_agent call) — stash its
                # transcript, seed a fresh instance from the handoff, and re-dispatch
                # — up to the same per-session cap. At the cap, fall through to the
                # is_error degrade below (result.output is None). The counter lives on
                # the shared top-level State (ctx.change_state); a nested dispatch has
                # none, so it falls through unchanged.
                if (result.handoff_path is not None
                        and ctx.change_state is not None
                        and ctx.change_state.reincarnation_count(reincarnation_key)
                        < ctx.change_state.max_reincarnations):
                    ctx.change_state.bump_reincarnation(reincarnation_key)
                    stashed = stash_transcript(target_dir)
                    # Note existence is the sole arbiter of handoff success;
                    # a missing note degrades the seed to transcript-only.
                    hp = Path(result.handoff_path)
                    note_exists = hp.exists()
                    reason = ("context threshold crossed" if note_exists
                              else "handoff failed — recovering from transcript")
                    ctx.event_log.append(
                        agent_id=None, type="reincarnating",
                        payload={"reason": reason})
                    seed = reincarnation_seed(
                        stashed_dir=stashed,
                        handoff_path=hp if note_exists else None)
                    _, target_dir = _allocate_subagent_dir(ctx, subagent_name)
                    sub_inv = _build_sub_inv(
                        target_dir,
                        (Message(role="user", content=(TextBlock(text=seed),)),))
                    continue
                # Tier B backstop: a subagent that hit the hard window error is reborn
                # INSIDE this dispatch (the parent still sees one resolved sub_agent
                # call) — stash its transcript, seed a fresh instance from it, and
                # re-dispatch — up to the per-session cap. An inner loop (not a
                # recursive dispatch_task call) avoids re-acquiring `limiter` and
                # deadlocking when the width cap is 1. The counter lives on the shared
                # top-level State (ctx.change_state); a nested dispatch has none, so it
                # falls through to today's failure path unchanged.
                if (result.fatal and result.error is not None
                        and result.error.category == "context_window"
                        and ctx.change_state is not None
                        and ctx.change_state.reincarnation_count(reincarnation_key)
                        < ctx.change_state.max_reincarnations):
                    ctx.change_state.bump_reincarnation(reincarnation_key)
                    stashed = stash_transcript(target_dir)
                    ctx.event_log.append(
                        agent_id=None, type="reincarnating",
                        payload={"reason": "context window exceeded"})
                    seed = reincarnation_seed(stashed_dir=stashed, handoff_path=None)
                    _, target_dir = _allocate_subagent_dir(ctx, subagent_name)
                    sub_inv = _build_sub_inv(
                        target_dir,
                        (Message(role="user", content=(TextBlock(text=seed),)),))
                    continue
                break

            if result.paused:
                # Unwind to the executor; resume restarts the subagent subtree fresh.
                raise SubagentSuspended("subagent paused")
            if result.fatal or result.errored or result.output is None:
                # The parent adjudicates a flaky subagent (is_error tool_result) — it
                # must not kill the task-lead. error_policy backoff has already been
                # applied above, so transient errors retried before reaching here.
                cat = result.error.category if result.error else "unknown"
                kind = result.error.kind if result.error else None
                # Tier A cap-exhausted: the subagent wrote a handoff but we have no
                # reincarnation budget left. No error object exists, so tag it explicitly.
                if kind is None and result.handoff_path is not None:
                    kind = "context_exhausted"
                # Embed the kind (e.g. "stalled" from the B.2 watchdog) so the parent's
                # is_error tool_result content carries it — Part D derives the subagent's
                # terminal glyph from this marker. Without it, a stalled subagent reads
                # as a generic ✗ failed and the operator can't tell it timed out.
                # CONSUMER CONTRACT: narrative._close_subagent_fold scans this error
                # content for the literal "[<kind>]" marker (only on is_error closes).
                # Keep the bracketed format and kind vocabulary in sync with that scan.
                suffix = f" [{kind}]" if kind else ""
                raise RuntimeError(f"subagent {subagent_name!r} failed ({cat}){suffix}")
            return cast(str, result.output["text"])
    finally:
        if spike_worktree is not None:
            from imp.engine import worktree as _worktree
            _worktree.destroy_spike_worktree(
                ctx.change.control_root, ctx.change.id, invocation_id)
