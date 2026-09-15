# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any

from imp.agents.dispatch import TurnState, dispatch_turn_tool_uses
from imp.agents.loop_guard import ToolOutcome, TurnRecord, _result_preview
from imp.agents.loop_items import (
    AwaitingToolResults,
    DependencyWaited,
    EndOfTurn,
    Error,
    Thinking,
    ToolRequested,
    Warning,
)
from imp.agents.output_guard import (
    Nudge,
    Ok,
    make_nudge_event,
    make_recovery_event,
    validate_output_or_nudge,
)
from imp.agents.runner import ToolUseBlock


async def run_agent_loop(
    invocation: Any, session: Any,
) -> AsyncGenerator[dict[str, Any], None]:
    """Drive one push `Session` to a terminal outer Event. Yields the same outer
    Event stream `drive_invocation` consumes (`agent_result` / `error` /
    `tool_heartbeat` / `handoff`)."""
    from imp.providers import errors as perr
    await session.open()
    while not session.cancelled():
        tool_reqs: list[ToolRequested] = []
        boundary: Any = None
        async for item in session.events():
            if isinstance(item, Error):
                yield {"type": "error", "payload": item.payload}
                return
            if isinstance(item, DependencyWaited):
                yield {"type": "dependency_wait",
                       "payload": {"depends_on": list(item.deps)}}
                return
            if isinstance(item, Thinking):
                session.elog.append(agent_id=session.aid, type="thinking",
                                    payload={"content": item.text})
                continue
            if isinstance(item, Warning):
                session.elog.append(agent_id=session.aid, type="runner_warning",
                                    payload={"message": item.text})
                continue
            if isinstance(item, ToolRequested):
                tool_reqs.append(item)
                continue
            if isinstance(item, (AwaitingToolResults, EndOfTurn)):
                boundary = item
                break

        if boundary is None:
            # events() ended with no boundary item -> the in-flight request was
            # cancelled; fall through to the interrupted error below.
            break

        # One post-response heartbeat per turn, matching the unconditional pulse
        # the old loops emitted after the model call (anthropic.py:803) BEFORE the
        # tool split -- so tool-less nudge/park turns keep pulsing the inactivity
        # watchdog (AC #6: same outer Event stream).
        yield {"type": "tool_heartbeat", "payload": {}}

        if isinstance(boundary, AwaitingToolResults):
            # Terminal point (a): execute the batch, then decide.
            tool_uses = [ToolUseBlock(id=r.id, name=r.name, input=r.input)
                         for r in tool_reqs]
            tool_results: list[Any] = []
            turn = TurnState(tool_calls_used=session.tool_calls_used)
            async for ev in dispatch_turn_tool_uses(
                    ctx=session.ctx, elog=session.elog, aid=session.aid,
                    invocation=invocation, tools=session.tools,
                    tool_uses=tool_uses, tool_results=tool_results,
                    state=turn, guard=session.guard, ceiling=session.ceiling):
                yield ev
                if ev.get("type") != "tool_heartbeat":
                    return  # complete_step / fatal / output_validation already yielded
            session.tool_calls_used = turn.tool_calls_used
            session.send_tool_results(tool_results)
            if turn.ceiling_tripped:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "tool_call_ceiling_exceeded",
                    "ceiling": session.ceiling,
                }}
                return
            if not turn.nudged:
                by_id = {r.id: r for r in tool_reqs}
                record = TurnRecord(
                    assistant_text=boundary.assistant_text,
                    tools=[
                        ToolOutcome(
                            name=by_id[b.tool_use_id].name if b.tool_use_id in by_id else "",
                            tool_use_id=b.tool_use_id,
                            input=by_id[b.tool_use_id].input if b.tool_use_id in by_id else {},
                            is_error=b.is_error,
                            result_preview=_result_preview(b.content))
                        for b in tool_results
                    ],
                    has_complete_step=False)
                loop_err = session.guard.observe(record)
                if loop_err is not None:
                    yield perr.make_error_event(loop_err, kind=getattr(loop_err, "kind", ""))
                    return
            action, path = session.handoff_after_tools()
            if action == "handoff":
                yield {"type": "handoff", "payload": {"handoff_path": path}}
                return
            if action == "injected":
                continue
            session.deliver_turn_boundary_interject()
            continue

        # Terminal point (b): EndOfTurn.
        last_text = boundary.last_text
        path = session.handoff_before_nudge(last_text)
        if path is not None:
            yield {"type": "handoff", "payload": {"handoff_path": path}}
            return
        # Observe terminal decisions (spec §3 terminal point b), written once
        # for every observe Session. Inert for push: PushSession.captured_complete_step()
        # is None and posthoc_ceiling_tripped() is False, so an anthropic/openai
        # EndOfTurn falls straight through to the subagent/continuation branches below.
        captured = session.captured_complete_step()
        if captured is not None:
            res = validate_output_or_nudge(
                captured, invocation.agent_def.output_schema, session.guard)
            if isinstance(res, Ok):
                if res.recovered is not None:
                    recovery_ev = make_recovery_event(res)
                    session.elog.append(agent_id=session.aid, type=recovery_ev["type"],
                                        payload=recovery_ev["payload"])
                yield {"type": "agent_result", "payload": res.payload}
                return
            if isinstance(res, Nudge):
                nudge_ev = make_nudge_event(res)
                session.elog.append(agent_id=session.aid, type=nudge_ev["type"],
                                    payload=nudge_ev["payload"])
                session.send(res.message, human=False)
                continue
            yield perr.make_error_event(res, kind=getattr(res, "kind", ""))
            return
        if session.posthoc_ceiling_tripped():
            yield {"type": "error", "payload": {
                "category": "config",
                "kind": "tool_call_ceiling_exceeded",
                "ceiling": session.ceiling,
            }}
            return
        if session.subagent_depth > 0:
            # Subagent bare-turn-end deliverable: last_assistant_text walks back
            # over empty trailing turns (anthropic.py:828), unlike this turn's
            # joined text -- align with the resume re-emit path.
            deliverable = session.subagent_deliverable()
            if deliverable:
                yield {"type": "agent_result", "payload": {"text": deliverable}}
            return
        cont = invocation.continuation_fn
        if cont is not None:
            next_text, human = cont(last_text)
            if inspect.isawaitable(next_text):
                next_text = await next_text
            if session.cancelled():
                # An interrupt landed while resolving the continuation: pause at
                # the turn boundary instead of writing a continuation the agent
                # was Ctrl-C'd out of (old claude.py:2511-2524).
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "interrupted",
                    "message": "runner cancelled by interrupt",
                }}
                return
            session.send(next_text, human=human)
            loop_err = session.observe_bare_turn(last_text)
            if loop_err is not None:
                yield perr.make_error_event(loop_err, kind=getattr(loop_err, "kind", ""))
                return
            session.deliver_turn_boundary_interject()
            continue
        yield {"type": "error", "payload": {
            "category": "agent_output_missing",
            "kind": "no_complete_step",
            "message": "agent finished without calling complete_step",
            "detail": {"last_agent_text": last_text[:500]},
        }}
        return

    yield {"type": "error", "payload": {
        "category": "config",
        "kind": "interrupted",
        "message": "runner cancelled by interrupt",
    }}
