"""query_events (spec §6.6). Reads events.jsonl from the current Change."""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.engine.events_digest import digest_event, full_line
from imp.engine.state import JsonlEventLog

_DEFAULT_LIMIT = 100


@imp_tool(
    name="query_events",
    description=(
        "The change's event log: the provenance record, in order, of what was "
        "said and what prior agents and reviews did on this change. Returns one "
        "compact digest line per event by default; pass full=True for raw event "
        "payloads (each capped at ~2 KB, marked when truncated). Narrow the "
        "result with type (an event type, or a comma-separated list of them), "
        "agent_id (a substring matched against an event's role/phase id), and "
        "limit (the last N events; default 100). A history channel, not a status "
        "one — for current state, prefer your durable working files."
    ),
)
def query_events(
    ctx: ToolContext,
    type: str | None = None,
    agent_id: str | None = None,
    limit: int | None = None,
    full: bool = False,
) -> str:
    log = JsonlEventLog(ctx.events_path)
    if limit is None:
        n = _DEFAULT_LIMIT
    else:
        # Coerce: some providers' tool schemas don't enforce numeric types,
        # so the LLM may emit `limit: "50"` as a quoted string.
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return json.dumps({"error": f"limit must be an integer, got {limit!r}"})
        # A non-positive limit would flood context with the whole file — clamp
        # to the bounded default instead.
        if n <= 0:
            n = _DEFAULT_LIMIT

    types = {t.strip() for t in type.split(",") if t.strip()} if type else None
    if types is None and not agent_id:
        events = log.tail(n)
    else:
        buf: deque[dict[str, Any]] = deque(maxlen=n)
        for e in log.read():
            if types is not None and e.get("type") not in types:
                continue
            if agent_id and agent_id not in (e.get("agent_id") or ""):
                continue
            buf.append(e)
        events = list(buf)

    render = full_line if full else digest_event
    return "\n".join(render(e) for e in events)
