"""read_file, write_file, edit_file (spec §6.6).

Permissions enforced inline (no backend layer). Virtual paths resolved via
ToolContext.resolve_path; /scratch/* values live in ctx.scratch.
"""

from __future__ import annotations

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


@imp_tool(
    name="read_file",
    description=(
        "Read a file's contents. "
        "Paths use /project/ or /scratch/ prefixes."
    ),
)
def read_file(ctx: ToolContext, path: str) -> str:
    ctx.check_fs_permission("read", path)
    resolved = ctx.resolve_path(path)
    if resolved is None:
        if path not in ctx.scratch:
            raise FileNotFoundError(f"{path!r} not in scratch")
        return ctx.scratch[path]
    return resolved.read_text()


@imp_tool(name="write_file", description="Write (overwrite) a file. Creates parent dirs as needed.")
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    ctx.check_fs_permission("write", path)
    resolved = ctx.resolve_path(path)
    if resolved is None:
        ctx.scratch[path] = content
        return f"wrote {len(content)} bytes to {path}"
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
    if resolved is None:
        body = ctx.scratch.get(path)
        if body is None:
            raise FileNotFoundError(f"{path!r} not in scratch")
    else:
        body = resolved.read_text()

    count = body.count(old)
    if count == 0:
        raise ValueError(f"{old!r} not found in {path!r}")
    if count > 1:
        raise ValueError(f"{old!r} not unique in {path!r} (found {count} occurrences)")
    new_body = body.replace(old, new, 1)
    if resolved is None:
        ctx.scratch[path] = new_body
    else:
        resolved.write_text(new_body)
    return f"replaced 1 occurrence in {path}"
