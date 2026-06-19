"""rename_change tool (spec §3, §10). Applies the rename IMMEDIATELY inside the
tool call and returns the settled handle so the agent uses it in-band, same turn.
Content-only: rewrites the gitignored change.json, touches no git. Top-level
only — raises for subagents (no change_state)."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine.slug import derive_handle, slugify
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="rename_change",
    description=(
        "Rename this Change to a meaningful name once you understand the work. "
        "Applies immediately and returns the new handle; use that handle to write "
        "and commit the spec at the change-home's artifacts/spec/<handle>.md this "
        "same turn."
    ),
)
def rename_change(ctx: Any, name: str) -> str:
    slugify(name)  # validate now (raises InvalidSlug -> ValueError) for fast feedback
    if ctx.change_state is None:
        raise RuntimeError("rename_change is only available to top-level Change steps")
    change = ctx.change
    new_handle = derive_handle(name, change.id)
    # No-op short-circuit on HANDLE equality, against the LIVE change.handle so a
    # second rename this turn compares against the first call's result.
    if new_handle == change.handle:
        return f"already named {new_handle}"
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    change.rename(name, log=log)
    return (f"renamed to {new_handle}; use "
            f"{change.change_dir}/artifacts/spec/{new_handle}.md for the spec")
