"""In-process streamable-HTTP MCP server for the codex provider (design
"Components → codex_mcp.py", "Concurrency / threading model — VALIDATED").

The server runs as an asyncio task on the runner's own event loop — never via
`mcp.run()` or `uvicorn.run()`. Each imp Tool is exposed through a thin async
wrapper that:
  - intercepts `complete_step` to capture args in `RunState` (the runner reads
    them at the next turn boundary, never via the call-stack return);
  - dispatches all other tools through `Tool.async_invoke(ctx, args)` against
    the live `ToolContext`;
  - checks the run-wide dispatch cap *before* doing work — the cap RMW is a
    single-coroutine read-modify-write with NO `await` between read and
    decision (invariant; preserves no-TOCTOU on the cap under interleaving).

`RunState` is shared with the runner's consume loop; both run on the same
event loop so locks are not required at `await` boundaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext


@dataclass
class RunState:
    """Shared state between the MCP tool wrappers and the runner's consume loop.

    `complete_step_args` is set inside the wrapper *before the response is
    serialized*, so the runner sees the captured args by the time the matching
    `turn.completed` event arrives (design "complete_step capture").

    `dispatch_count` and `cap` are the whole-`run()` budget (NOT per-turn). One
    MCP server spans all in-turn nudges/resumes (design "Cap scope").

    `abort_requested` is set whenever a cap trip fires; the runner observes it
    and routes the abort through `terminate_running_processes(ctx)` so
    governed `bash` children don't leak (N2).
    """
    cap: int
    dispatch_count: int = 0
    complete_step_called: bool = False
    complete_step_args: dict[str, Any] = field(default_factory=dict)
    abort_requested: bool = False
    runtime_errors: list[str] = field(default_factory=list)


def _cap_error_text(cap: int) -> str:
    return (
        f"tool_call_ceiling_exceeded (cap={cap}); run is being aborted. "
        "No further tool calls will be honored."
    )


async def dispatch_tool(
    *, tool: Tool, args: dict[str, Any], ctx: ToolContext, state: RunState,
) -> str:
    """Invoke `tool` against `ctx` with `args`, returning a textual result
    suitable for an MCP tool response body.

    Cap RMW invariant: the count is incremented and compared synchronously
    here — there is NO `await` between the read (`state.dispatch_count`) and
    the decision (`> state.cap`). Future maintainers MUST keep these two lines
    adjacent and synchronous.
    """
    # ── cap RMW (no await between read and decision) ──────────────────────────
    state.dispatch_count += 1
    if state.dispatch_count > state.cap:
        state.abort_requested = True
        return _cap_error_text(state.cap)
    # ─────────────────────────────────────────────────────────────────────────
    if tool.name == "complete_step":
        # Capture args; return OK so codex moves on to turn.completed. The
        # runner reads `state.complete_step_args` at that boundary, not via
        # a call-stack return (design "complete_step capture").
        state.complete_step_called = True
        state.complete_step_args = dict(args)
        return "step complete"
    try:
        raw = await tool.async_invoke(ctx, args)
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        state.runtime_errors.append(text)
        return text
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return str(raw)
