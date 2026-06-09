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
from dataclasses import dataclass
from typing import Any

from imp.agents.runner import ToolResultBlock, ToolUseBlock
from imp.agents.subagents import dispatch_task, raise_if_suspended
from imp.agents.tools import Tool
from imp.agents.tools.complete_step import StepComplete
from imp.agents.tools.errors import FatalToolError
from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.engine.schema import validate_agent_output


@dataclass
class TurnState:
    """Mutable per-assistant-turn dispatch state, shared with the caller."""

    tool_calls_used: int
    ceiling_tripped: bool = False


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
    ceiling: int | None,
) -> dict[str, Any] | None:
    """Execute ONE tool_use block.

    Returns None for every non-terminal arm (a tool_result was appended to
    tool_results + elog; the caller yields the heartbeat), or a terminal event
    dict the caller must yield and then return. AwaitingUserInput and
    subagent-suspension exceptions propagate (terminal — no heartbeat; the
    generator ends by raising).
    """
    if ceiling is not None and state.tool_calls_used >= ceiling:
        # Backfill error tool_results to preserve the transcript invariant.
        err_text = f"tool_call_ceiling_exceeded (ceiling={ceiling})"
        block = ToolResultBlock(tu.id, err_text, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
        state.ceiling_tripped = True
        return None
    state.tool_calls_used += 1

    tc_ev = elog.append_tool_call(agent_id=aid, block=tu)

    # task special-case: bypasses the sync Tool.invoke path entirely — go
    # straight to the async dispatcher. Use the persisted tool_call event id
    # for the durable parent link.
    if tu.name == "task":
        try:
            result_str = await dispatch_task(
                ctx, tu.input["subagent_name"], tu.input["task_description"],
                tool_call_event_id=tc_ev["id"],
                tier=tu.input.get("tier"),
            )
        except Exception as e:
            raise_if_suspended(e)  # unwind a subagent-origin pause
            err_text = f"{type(e).__name__}: {e}"
            block = ToolResultBlock(tu.id, err_text, is_error=True)
            tool_results.append(block)
            elog.append_tool_result(agent_id=aid, block=block)
            return None
        block = ToolResultBlock(tu.id, result_str)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
        return None

    tool = tools.get(tu.name)
    if tool is None:
        msg = f"unknown tool {tu.name!r}"
        block = ToolResultBlock(tu.id, msg, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
        return None
    try:
        raw_result = await tool.async_invoke(ctx, tu.input)
    except StepComplete as sc:
        try:
            validate_agent_output(sc.args_payload, invocation.agent_def.output_schema)
        except Exception as ve:
            return {"type": "error", "payload": {
                "category": "agent_output_missing",
                "kind": "output_validation",
                "message": str(ve),
            }}
        return {"type": "agent_output", "payload": sc.args_payload}
    except FatalToolError as fte:
        return {"type": "error", "payload": {
            "category": "config",
            "kind": "fatal_tool_error",
            "tool": tu.name,
            "message": str(fte),
        }}
    except AwaitingUserInput:
        # Headless wait_for_user pause: must unwind out of run() (like
        # SubagentSuspended), not become an is_error tool_result. The
        # assistant message bearing this tool_use was already appended, so the
        # transcript ends at the dangling wait_for_user tool_use.
        raise
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        block = ToolResultBlock(tu.id, err_text, is_error=True)
        tool_results.append(block)
        elog.append_tool_result(agent_id=aid, block=block)
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
    elog.append_tool_result(agent_id=aid, block=block)
    return None
