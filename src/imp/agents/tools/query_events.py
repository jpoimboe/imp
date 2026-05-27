"""query_events (spec §6.6). Reads events.jsonl from the current Change."""

from __future__ import annotations

import json

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import EventLog


@imp_tool(
    name="query_events",
    description="Return events.jsonl as JSON. Optionally filter by type, limit to last N.",
)
def query_events(ctx: ToolContext, type: str | None = None, limit: int | None = None) -> str:
    events = list(EventLog(ctx.events_path).read())
    if type:
        events = [e for e in events if e.get("type") == type]
    if limit is not None:
        # Coerce: some providers' tool schemas don't enforce numeric types,
        # so the LLM may emit `limit: "50"` as a quoted string.
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            return json.dumps({"error": f"limit must be an integer, got {limit!r}"})
        if limit_int > 0:
            events = events[-limit_int:]
    return json.dumps(events, default=str)
