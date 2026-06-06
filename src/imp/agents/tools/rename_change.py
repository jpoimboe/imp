"""rename_change tool (spec §3, §10). Applies the rename IMMEDIATELY inside the
tool call and returns the settled slug so the agent uses it in-band, same turn.
Content-only: rewrites the gitignored change.json, touches no git. Top-level
only — raises for subagents (no change_state)."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine.slug import dated_slug, slugify
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="rename_change",
    description=(
        "Rename this Change to a meaningful name once you understand the work. "
        "Applies immediately and returns the new slug; use that slug to write "
        "and commit imp/specs/<slug>.md this same turn."
    ),
)
def rename_change(ctx: Any, name: str) -> str:
    slugify(name)  # validate now (raises InvalidSlug -> ValueError) for fast feedback
    if ctx.change_state is None:
        raise RuntimeError("rename_change is only available to top-level Change steps")
    change = ctx.change
    new_slug = dated_slug(name, date=change.id[:10])
    # No-op short-circuit on SLUG equality, against the LIVE change.slug (not the
    # frozen render-time snapshot) so a second rename this turn compares against
    # the first call's result. Slug-equality (not title) is the cache-stability test.
    if new_slug == change.slug:
        return f"already named {new_slug}"
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    change.rename(name, log=log)
    return (f"renamed to {new_slug}; use imp/specs/{new_slug}.md for the spec")
