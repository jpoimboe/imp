from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool


@imp_tool(
    name="emit_notice",
    description=(
        "Print a one-time multi-line notice block to the live view. Pass the "
        "fully-formatted text; it is shown verbatim, once, in the scrollback."
    ),
)
def emit_notice(ctx: Any, text: str) -> str:
    if ctx.event_log is not None:
        ctx.event_log.append(
            agent_id=getattr(ctx, "agent_id", None),
            type="notice",
            payload={"text": text},
        )
    return "notice emitted"
