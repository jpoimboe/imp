"""rename_change tool (spec §3, §10). Records the requested rename; the
executor applies it at the next step boundary under the git-lock."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine.slug import slugify


@imp_tool(
    name="rename_change",
    description=(
        "Rename this Change to a meaningful name once you understand the work. "
        "Takes effect at the next step boundary; the run continues either way."
    ),
)
def rename_change(ctx: Any, name: str) -> str:
    slugify(name)  # validate now (raises InvalidSlug -> ValueError) for fast feedback
    if ctx.change_state is None:
        raise RuntimeError("rename_change is only available to top-level Change steps")
    ctx.change_state.pending_rename = name
    return (
        f"Rename to {name!r} recorded; it will be applied at the next step "
        "boundary (slug derived automatically)."
    )
