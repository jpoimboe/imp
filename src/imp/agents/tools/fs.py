"""read_file, write_file, edit_file (spec §6.6).

Permissions enforced inline (no backend layer). Paths are worktree-RELATIVE and
resolved via ToolContext.resolve_path, which enforces worktree containment.
"""

from __future__ import annotations

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import (
    OUTPUT_TRUNCATION_MARKER,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    read_text_capped,
)


@imp_tool(
    name="read_file",
    description=(
        "Read a file's contents (bounded — output is capped at 50 KB). "
        "Paths are relative to the project root (e.g. src/foo.py)."
    ),
)
async def read_file(ctx: ToolContext, path: str) -> str:
    ctx.check_fs_permission("read", path)
    resolved = ctx.resolve_path(path)
    text, truncated = await read_text_capped(
        resolved,
        coordinator=getattr(ctx, "coordinator", None),
        deadline=anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S,
        cap_bytes=TOOL_OUTPUT_CAP_BYTES,   # 50 KB — same cap bash/grep apply
    )
    if truncated:
        text += OUTPUT_TRUNCATION_MARKER
    return text


@imp_tool(name="write_file", description="Write (overwrite) a file. Creates parent dirs as needed.")
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    ctx.check_fs_permission("write", path)
    resolved = ctx.resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    return f"wrote {len(content)} bytes to {path}"


@imp_tool(
    name="edit_file",
    description=(
        "Replace a single unique occurrence of `old` with `new` in `path`. "
        "Raises if `old` not found or not unique."
    ),
)
def edit_file(ctx: ToolContext, path: str, old: str, new: str) -> str:
    ctx.check_fs_permission("read", path)
    ctx.check_fs_permission("write", path)
    resolved = ctx.resolve_path(path)
    body = resolved.read_text()
    count = body.count(old)
    if count == 0:
        raise ValueError(f"{old!r} not found in {path!r}")
    if count > 1:
        raise ValueError(f"{old!r} not unique in {path!r} (found {count} occurrences)")
    resolved.write_text(body.replace(old, new, 1))
    return f"replaced 1 occurrence in {path}"
