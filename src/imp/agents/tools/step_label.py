from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool


@imp_tool(
    name="update_step_label",
    description=(
        "Update the live status-bar label for the current step. Pass the full "
        "label string to display; it is shown verbatim and persists until the "
        "next call. Use this to surface live progress within a step."
    ),
)
def update_step_label(ctx: Any, label: str) -> str:
    if ctx.event_log is not None:
        ctx.event_log.append(
            agent_id=getattr(ctx, "agent_id", None),
            type="step_label_updated",
            payload={"label": label},
        )
    return "status label updated"
