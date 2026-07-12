"""imp_show — read another quest's durable overview or spec by id. Reads the
current quest by default, or another quest via quest_id."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext

_DESCRIPTION = (
    "Read another quest's durable record by id. `overview` (the default) returns "
    "its detail card — id, title, status, timing, dependencies, the opening "
    "intent, and its summary inlined — for both live and archived quests. `spec` "
    "returns its raw spec. Defaults to the current quest when no `quest_id` is "
    "given. Returns a JSON `{\"error\": …}` string for an unknown id or a facet "
    "that doesn't exist yet. For the event timeline, use `imp_events`; this reads "
    "the distilled record, not the raw log."
)


@imp_tool(name="imp_show", description=_DESCRIPTION, read_only=True)
def imp_show(
    ctx: ToolContext,
    resource: Literal["overview", "spec"] = "overview",
    quest_id: str | None = None,
) -> str:
    # Lazy import: the card-building cluster is the single source of truth for
    # `imp show`; importing imp.cli.main here (not at module top) keeps the
    # agent-tools layer free of a load-time dependency on the CLI/TUI layer.
    from imp.cli import main as cli_main

    current_id = ctx.events_path.parent.name
    # Truthy check mirrors query_events: a blank quest_id routes to the current
    # quest rather than reaching resolution with an empty id.
    target = quest_id if quest_id else current_id
    # <control>/.imp/quests/<id>/events.jsonl -> parents[3] == <control>.
    control_root = ctx.events_path.parents[3]

    if resource == "spec":
        data = cli_main._show_artifact(
            control_root, target, f"spec/{target}.md")
        if data is None:
            return json.dumps({"error": f"no spec for {target!r}"})
        return data.decode("utf-8", errors="replace")

    lines = cli_main._detail_card(
        control_root, target, now=datetime.now(UTC))
    if not lines:
        return json.dumps({"error": f"no such quest {target!r}"})
    return "\n".join(lines)
