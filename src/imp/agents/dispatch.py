"""Shared per-tool_use dispatch body for the in-process providers.

anthropic and openai run verbatim-sibling tool loops that have already shipped
one drift-induced parity violation in this codebase. The per-tu body also has
six early arms (`continue`s for ceiling backfill / task success / task error /
unknown tool / generic tool exception, plus terminal `return`s), which collide
with the heartbeat contract: a single `yield` at the loop bottom would be
skipped by every `continue`, silently violating exactly the arms the contract
names. Hoisting the body here lets both providers yield exactly one
`tool_heartbeat` per non-terminal tool_use AROUND the call — and gives the
background sweep one shared, drift-proof call site (Task 12).

Cycle note: this module imports agents.subagents (which imports
engine.agent_run); nothing here may be imported BY agent_run or subagents.
Only the providers import this module.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio

from imp.agents.loop_guard import LoopGuard
from imp.agents.output_guard import (
    Nudge,
    Ok,
    make_nudge_event,
    make_recovery_event,
    validate_output_or_nudge,
)
from imp.agents.parallel import max_parallel_tasks
from imp.agents.runner import ToolResultBlock, ToolUseBlock
from imp.agents.subagents import (
    dispatch_task,
    raise_if_suspended,
)
from imp.agents.tools import Tool
from imp.agents.tools import bg as _bg
from imp.agents.tools.complete_step import StepComplete
from imp.agents.tools.context import suspend_inactivity_watchdog
from imp.agents.tools.errors import FatalToolError
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.schema import validate_agent_output


@dataclass
class TurnState:
    """Mutable per-assistant-turn dispatch state, shared with the caller."""

    tool_calls_used: int
    ceiling_tripped: bool = False
    nudged: bool = False


async def dispatch_one_tool_use(
    *,
    ctx: Any,
    elog: Any,
    aid: str,
    invocation: Any,
    tools: dict[str, Tool],
    tu: ToolUseBlock,
    tool_results: list[ToolResultBlock],
    state: TurnState,
    guard: LoopGuard | None = None,
    ceiling: int | None,
) -> dict[str, Any] | None:
    """Execute ONE tool_use block.

    Returns None for every non-terminal arm (a tool_result was appended to
    tool_results + elog; the caller yields the heartbeat), or a terminal event
    dict the caller must yield and then return. AwaitingUserInput and
    subagent-suspension exceptions propagate (terminal — no heartbeat; the
    generator ends by raising).
    """
    if tu.name == "sub_agent":
        # §1: task tus route through dispatch_task_batch (concurrent path) or the
        # kill-switch inline path, never here. raise (NOT assert) so a routing
        # regression is caught even under `python -O`.
        raise RuntimeError(
            "task tu reached dispatch_one_tool_use; tasks must route through "
            "dispatch_task_batch / the inline kill-switch path (dispatch.py)")

    # Decision 10 sweep site: EVERY tool dispatch — all tools, not just the
    # bash family. An agent that backgrounds a server and then spends the
    # invocation in read_file/grep/edit_file must still be swept. Module-attr
    # call so tests can monkeypatch bg._sweep_bg once for all sites.
    await _bg._sweep_bg(ctx)

    if ceiling is not None and state.tool_calls_used >= ceiling:
        # Backfill error tool_results to preserve the transcript invariant.
        err_text = f"tool_call_ceiling_exceeded (ceiling={ceiling})"
        block = ToolResultBlock(tu.id, err_text, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
        state.ceiling_tripped = True
        return None
    state.tool_calls_used += 1

    elog.append_tool_call(agent_id=aid, block=tu)

    tool = tools.get(tu.name)
    if tool is None:
        msg = f"unknown tool {tu.name!r}"
        block = ToolResultBlock(tu.id, msg, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
        return None
    start = time.perf_counter()
    try:
        with suspend_inactivity_watchdog(ctx):
            raw_result = await tool.async_invoke(ctx, tu.input)
    except StepComplete as sc:
        if guard is None:
            # No loop guard (e.g. the scripted provider): in-session nudging needs
            # a guard to be bounded, so preserve today's terminal
            # validate-or-error behavior.
            try:
                validate_agent_output(
                    sc.args_payload, invocation.agent_def.output_schema)
            except Exception as ve:
                return {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "output_validation",
                    "message": str(ve),
                }}
            return {"type": "agent_output", "payload": sc.args_payload}
        res = validate_output_or_nudge(
            sc.args_payload, invocation.agent_def.output_schema, guard)
        if isinstance(res, Ok):
            if res.recovered is not None:
                ev = make_recovery_event(res)
                elog.append(agent_id=aid, type=ev["type"], payload=ev["payload"])
            return {"type": "agent_output", "payload": res.payload}
        if isinstance(res, Nudge):
            block = ToolResultBlock(tu.id, res.message, is_error=True)
            tool_results.append(block)
            elog.append_tool_result(
                agent_id=aid, block=block,
                duration_ms=(time.perf_counter() - start) * 1000.0)
            ev = make_nudge_event(res)
            elog.append(agent_id=aid, type=ev["type"], payload=ev["payload"])
            state.nudged = True
            return None
        # LoopError: the guard tripped on repeated/no-progress malformed
        # completions — surface the terminal loop event (pause-to-human).
        from imp.providers.errors import make_error_event
        return make_error_event(res, kind=getattr(res, "kind", ""))
    except FatalToolError as fte:
        return {"type": "error", "payload": {
            "category": "config",
            "kind": "fatal_tool_error",
            "tool": tu.name,
            "message": str(fte),
        }}
    except AwaitingUserInput:
        # Headless bare-turn-end yield: must unwind out of run() (like
        # SubagentSuspended), not become an is_error tool_result. The assistant
        # message has already been appended, so the transcript ends at the
        # agent's last text — the bare turn-end that is the yield.
        raise
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        block = ToolResultBlock(tu.id, err_text, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block,
                                duration_ms=(time.perf_counter() - start) * 1000.0)
        return None

    content_str = (raw_result if isinstance(raw_result, str)
                   else json.dumps(raw_result, default=str))
    if tu.name == "bash":
        # rc<0 -> tool_killed heuristic. Gated on tu.name == "bash" so it
        # never sees bash_kill's result: an agent-requested kill is EXPECTED
        # (spec Decision 6); the negative returncode flows back as plain data.
        try:
            parsed = json.loads(content_str)
        except (TypeError, ValueError):
            parsed = {}
        rc = parsed.get("returncode")
        if isinstance(rc, int) and rc < 0:
            elog.append(agent_id=aid, type="tool_killed",
                        payload={"id": tu.id, "name": "bash",
                                 "signal": -rc})
    block = ToolResultBlock(tu.id, content_str)
    tool_results.append(block)
    elog.append_tool_result(agent_id=aid, block=block,
                            duration_ms=(time.perf_counter() - start) * 1000.0)
    return None


async def dispatch_task_batch(
    *,
    ctx: Any,
    elog: Any,
    aid: str,
    task_tus: list[ToolUseBlock],
    tool_results: list[ToolResultBlock],
    state: TurnState,
    ceiling: int | None,
) -> AsyncIterator[dict[str, Any]]:
    """Dispatch every `task` tu of one turn CONCURRENTLY in an anyio task group
    (design §1). Sweep ONCE before the batch (children sweep their own
    registries); claim ceiling slots synchronously in document order
    (over-ceiling tus get the backfill is_error tool_result event with NO
    tool_call event — today's result-without-call shape); append each dispatched
    tu's tool_call event before the group starts; run the children; append each
    child's tool_result at its own completion. Results are assembled into
    tool_results in DOCUMENT order (keyed off task_tus, not completion arrival).
    After the group settles, yield one tool_heartbeat per task tu (dispatched AND
    backfilled).

    Failure isolation: a child's exception — ordinary OR `SubagentSuspended` —
    becomes its own `is_error` tool_result; only a run-wide hard-cancel tears
    the whole batch down via the group.
    """
    # Sweep once before the batch — the parent's registry, not the children's
    # (each child sweeps its own fresh ToolContext inside its own drive loop).
    await _bg._sweep_bg(ctx)

    dispatched: list[tuple[ToolUseBlock, str]] = []
    results: dict[str, ToolResultBlock] = {}
    for tu in task_tus:
        if ceiling is not None and state.tool_calls_used >= ceiling:
            err_text = f"tool_call_ceiling_exceeded (ceiling={ceiling})"
            block = ToolResultBlock(tu.id, err_text, is_error=True)
            results[tu.id] = block
            elog.append_tool_result(agent_id=aid, block=block)
            state.ceiling_tripped = True
        else:
            state.tool_calls_used += 1
            sub_def = ctx.subagent_defs.get(tu.input["subagent_name"])
            label = sub_def.label if sub_def is not None else None
            # Tag the dispatch block-tracked: its result below carries id_ns="block",
            # so the fold stays open until then rather than being resume-closed.
            tc_ev = elog.append_tool_call(agent_id=aid, block=tu, label=label,
                                          id_ns="block")
            dispatched.append((tu, tc_ev["id"]))

    async def _run_child(tu: ToolUseBlock, tcid: str) -> None:
        try:
            result_str = await dispatch_task(
                ctx, tu.input["subagent_name"], tu.input["task_description"],
                tool_call_event_id=tcid, tier=tu.input.get("tier"),
            )
        except Exception as e:
            # a batched child cannot pause the run. SubagentSuspended
            # (backoff-ceiling exhaustion / mid-batch wait) is an Exception
            # subclass, so it degrades to this child's own is_error tool_result
            # alongside ordinary failures; siblings are unaffected. Only a
            # run-wide Ctrl-C (a BaseException, propagated by the group) cancels
            # the whole batch via the outer checkpoint.
            err_text = f"{type(e).__name__}: {e}"
            block = ToolResultBlock(tu.id, err_text, is_error=True)
            results[tu.id] = block
            elog.append_tool_result(agent_id=aid, block=block, id_ns="block")
            return
        block = ToolResultBlock(tu.id, result_str)
        results[tu.id] = block
        elog.append_tool_result(agent_id=aid, block=block, id_ns="block")

    try:
        async with anyio.create_task_group() as tg:
            for tu, tcid in dispatched:
                tg.start_soon(_run_child, tu, tcid)
    except BaseExceptionGroup as eg:
        # A surrounding hard-cancel (watchdog / run-wide Ctrl-C) surfaces as a
        # BaseException the children do NOT catch; anyio wraps even a single one
        # in a group. Unwrap the single-exception case so the classifier sees
        # today's exception forms; genuinely-multiple simultaneous failures
        # re-raise as-is.
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None
        raise

    # Assemble in document order (NOT completion order): the API pairs by id, but
    # the assembled message order is imp's to choose and document order is the one
    # §3's reconstruction mirrors.
    for tu in task_tus:
        b = results.get(tu.id)
        if b is not None:
            tool_results.append(b)

    # Heartbeat contract: exactly one per task tu, after the batch (including
    # over-ceiling backfilled tus); the generator cannot yield while suspended in
    # the group await, so they land together here.
    for _ in task_tus:
        yield {"type": "tool_heartbeat", "payload": {}}


def _reorder_results_to_document_order(
    tool_results: list[ToolResultBlock], tool_uses: list[ToolUseBlock],
) -> None:
    """Reorder the turn's tool_results in place to match tool_use DOCUMENT order
    (design §1/§3). Batch execution is completion-ordered, but the turn's final
    user message pairs results to tool_uses by id, and document order is the one
    §3's reconstruction mirrors. Stable sort; an unknown id (defensive) sorts
    last."""
    order = {tu.id: i for i, tu in enumerate(tool_uses)}
    tool_results.sort(key=lambda b: order.get(b.tool_use_id, len(order)))


async def _dispatch_one_task_inline(
    *,
    ctx: Any,
    elog: Any,
    aid: str,
    tu: ToolUseBlock,
    tool_results: list[ToolResultBlock],
    state: TurnState,
    ceiling: int | None,
) -> None:
    """Kill-switch inline dispatch of ONE task tu — today's sequential shapes
    exactly (sweep, ceiling, tool_call, dispatch_task, tool_result). Reconstructs
    the task arm removed from dispatch_one_tool_use; a SubagentSuspended unwinds
    the turn as it does today (raise_if_suspended re-raises)."""
    await _bg._sweep_bg(ctx)
    if ceiling is not None and state.tool_calls_used >= ceiling:
        err_text = f"tool_call_ceiling_exceeded (ceiling={ceiling})"
        block = ToolResultBlock(tu.id, err_text, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
        state.ceiling_tripped = True
        return
    state.tool_calls_used += 1
    sub_def = ctx.subagent_defs.get(tu.input["subagent_name"])
    label = sub_def.label if sub_def is not None else None
    # Tag the dispatch block-tracked: its result below carries id_ns="block", so the
    # fold stays open until then rather than being resume-closed.
    tc_ev = elog.append_tool_call(agent_id=aid, block=tu, label=label,
                                  id_ns="block")
    try:
        result_str = await dispatch_task(
            ctx, tu.input["subagent_name"], tu.input["task_description"],
            tool_call_event_id=tc_ev["id"], tier=tu.input.get("tier"),
        )
    except Exception as e:
        raise_if_suspended(e)
        err_text = f"{type(e).__name__}: {e}"
        block = ToolResultBlock(tu.id, err_text, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block, id_ns="block")
        return
    block = ToolResultBlock(tu.id, result_str)
    tool_results.append(block)
    elog.append_tool_result(agent_id=aid, block=block, id_ns="block")


async def dispatch_turn_tool_uses(
    *,
    ctx: Any,
    elog: Any,
    aid: str,
    invocation: Any,
    tools: dict[str, Tool],
    tool_uses: list[ToolUseBlock],
    tool_results: list[ToolResultBlock],
    state: TurnState,
    guard: LoopGuard | None = None,
    ceiling: int | None,
) -> AsyncIterator[dict[str, Any]]:
    """Dispatch one assistant turn's tool_use blocks (design §1). Partition: ALL
    `task` tus run as one concurrent batch at the FIRST task tu's document
    position; every non-task tu flows through `dispatch_one_tool_use` in document
    order; the batch never overlaps a non-task tool. After all dispatch, the
    turn's tool_results are reordered to document order. Yields one tool_heartbeat
    per processed tu plus any terminal event dict (the caller yields it and
    stops). SubagentSuspended / AwaitingUserInput propagate (terminal — no
    heartbeat; the generator ends by raising)."""
    if max_parallel_tasks() == 1:
        # Kill switch (Decision 5): inline document-order path, no task group —
        # today's shapes exactly. Each tu (task and non-task) dispatches
        # sequentially in document order; results are already document-ordered, so
        # no reorder is applied (and none is needed).
        for tu in tool_uses:
            if tu.name == "sub_agent":
                await _dispatch_one_task_inline(
                    ctx=ctx, elog=elog, aid=aid, tu=tu,
                    tool_results=tool_results, state=state, ceiling=ceiling)
            else:
                terminal = await dispatch_one_tool_use(
                    ctx=ctx, elog=elog, aid=aid, invocation=invocation,
                    tools=tools, tu=tu, tool_results=tool_results,
                    state=state, guard=guard, ceiling=ceiling)
                if terminal is not None:
                    yield terminal
                    return
            yield {"type": "tool_heartbeat", "payload": {}}
        return

    task_tus = [tu for tu in tool_uses if tu.name == "sub_agent"]
    batch_started = False
    for tu in tool_uses:
        if tu.name == "sub_agent":
            if not batch_started:
                batch_started = True
                async for ev in dispatch_task_batch(
                        ctx=ctx, elog=elog, aid=aid, task_tus=task_tus,
                        tool_results=tool_results, state=state, ceiling=ceiling):
                    yield ev
            # later task tus are already handled by the single batch
            continue
        terminal = await dispatch_one_tool_use(
            ctx=ctx, elog=elog, aid=aid, invocation=invocation,
            tools=tools, tu=tu, tool_results=tool_results,
            state=state, guard=guard, ceiling=ceiling)
        if terminal is not None:
            yield terminal
            return
        yield {"type": "tool_heartbeat", "payload": {}}

    _reorder_results_to_document_order(tool_results, tool_uses)
