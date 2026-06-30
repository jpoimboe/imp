"""query_events (spec §6.6). Reads events.jsonl from the current Change."""

from __future__ import annotations

import json

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import JsonlEventLog

_DEFAULT_LIMIT = 100


@imp_tool(
    name="query_events",
    description=(
        "Return events.jsonl as JSONL (one JSON event per line, chunkable and "
        "greppable). Optionally filter by type. `limit` returns the last N events; "
        "with no limit, returns the last 100."
    ),
)
def query_events(ctx: ToolContext, type: str | None = None, limit: int | None = None) -> str:
    events = list(JsonlEventLog(ctx.events_path).read())
    if type:
        events = [e for e in events if e.get("type") == type]
    if limit is None:
        events = events[-_DEFAULT_LIMIT:]
    else:
        # Coerce: some providers' tool schemas don't enforce numeric types,
        # so the LLM may emit `limit: "50"` as a quoted string.
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            return json.dumps({"error": f"limit must be an integer, got {limit!r}"})
        if limit_int > 0:
            events = events[-limit_int:]
    return "\n".join(json.dumps(e, default=str) for e in events)
