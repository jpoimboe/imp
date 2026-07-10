"""query_events (spec §6.6). Reads a Quest's event timeline — the current
change by default, or another change via quest_id."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.engine.archive import read as archive_read
from imp.engine.errors import LoadError
from imp.engine.events_digest import digest_event, full_line
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, parse_events_jsonl

_DEFAULT_LIMIT = 100


def _resolve_other_change_events(
    control_root: Path, quest_id: str,
) -> list[dict[str, Any]] | None:
    """Worktree-first (Quest.load), then archive (archive_read.read_events),
    mirroring the CLI's _load_events_for_id. None if the id is in neither."""
    try:
        change = Quest.load(control_root, quest_id)
    except LoadError:
        change = None
    if change is not None:
        return list(change.event_log().read())
    raw = archive_read.read_events(control_root, quest_id)
    if raw is None:
        return None
    return parse_events_jsonl(raw)


def _filter_and_limit(
    events: Iterable[dict[str, Any]],
    types: set[str] | None,
    agent_id: str | None,
    n: int,
) -> list[dict[str, Any]]:
    buf: deque[dict[str, Any]] = deque(maxlen=n)
    for e in events:
        if types is not None and e.get("type") not in types:
            continue
        if agent_id and agent_id not in (e.get("agent_id") or ""):
            continue
        buf.append(e)
    return list(buf)


@imp_tool(
    name="query_events",
    description=(
        "The change's event log: the provenance record, in order, of what was "
        "said and what prior agents and reviews did on this change. Returns one "
        "compact digest line per event by default; pass full=True for raw event "
        "payloads (each capped at ~2 KB, marked when truncated). Narrow the "
        "result with type (an event type, or a comma-separated list of them), "
        "agent_id (a substring matched against an event's role/phase id), and "
        "limit (the last N events; default 100). By default it reads the current "
        "change; pass quest_id to read another change's timeline. A history "
        "channel, not a status one — for current state, prefer your durable "
        "working files."
    ),
)
def query_events(
    ctx: ToolContext,
    type: str | None = None,
    agent_id: str | None = None,
    limit: int | None = None,
    full: bool = False,
    quest_id: str | None = None,
) -> str:
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

    current_id = ctx.events_path.parent.name
    # Truthy (not `is not None`): a blank quest_id="" routes to the current-change
    # path — otherwise it would reach Quest.load, where <control>/.imp/quests is a
    # real dir so .is_dir() passes and the missing quest.json raises FileNotFoundError,
    # escaping the LoadError catch and breaking the "does not raise" contract.
    if quest_id and quest_id != current_id:
        # <control>/.imp/quests/<id>/events.jsonl -> parents[3] == <control>.
        control_root = ctx.events_path.parents[3]
        resolved = _resolve_other_change_events(control_root, quest_id)
        if resolved is None:
            return json.dumps({"error": f"no such change {quest_id!r}"})
        events = _filter_and_limit(resolved, types, agent_id, n)
    else:
        log = JsonlEventLog(ctx.events_path)
        if types is None and not agent_id:
            events = log.tail(n)
        else:
            events = _filter_and_limit(log.read(), types, agent_id, n)

    render = full_line if full else digest_event
    return "\n".join(render(e) for e in events)
