"""glob, grep, ls (spec §6.6).

Read-only navigation. Permissions: only check 'read' on the resolved base
path (top of the pattern's prefix) — globbing 1000 files shouldn't issue
1000 permission checks.
"""

from __future__ import annotations

import re
from pathlib import Path

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


def _virtual_prefix(path: str) -> tuple[str, str]:
    """Returns (prefix, remainder). Prefix is /project."""
    if path.startswith("/project"):
        return "/project", path[len("/project"):].lstrip("/")
    raise ValueError(f"unsupported virtual path: {path!r}")


def _real_root(ctx: ToolContext, prefix: str) -> Path:
    return ctx.project_root


@imp_tool(
    name="glob",
    description="List file paths matching a virtual-prefixed glob (e.g. /project/**/*.py).",
)
def glob(ctx: ToolContext, pattern: str) -> str:
    prefix, rel = _virtual_prefix(pattern)
    ctx.check_fs_permission("read", prefix + "/")
    root = _real_root(ctx, prefix)
    matches = sorted(root.glob(rel))
    if not matches:
        return "(no matches)"
    out: list[str] = []
    for p in matches:
        out.append(f"{prefix}/{p.relative_to(root).as_posix()}")
    return "\n".join(out)


@imp_tool(
    name="grep",
    description=(
        "Search for a regex pattern in files under a virtual path. "
        "Returns matching lines with file paths."
    ),
)
def grep(ctx: ToolContext, pattern: str, path: str = "/project") -> str:
    prefix, rel = _virtual_prefix(path)
    ctx.check_fs_permission("read", prefix + "/")
    root = _real_root(ctx, prefix)
    target = root / rel if rel else root
    rgx = re.compile(pattern)
    out: list[str] = []
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rgx.search(line):
                rel_str = f.relative_to(root).as_posix()
                out.append(f"{prefix}/{rel_str}:{i}:{line}")
    return "\n".join(out) if out else "(no matches)"


@imp_tool(name="ls", description="List directory contents at a virtual path.")
def ls(ctx: ToolContext, path: str) -> str:
    prefix, rel = _virtual_prefix(path)
    ctx.check_fs_permission("read", path)
    root = _real_root(ctx, prefix)
    target = root / rel if rel else root
    if not target.exists():
        return f"(no such directory: {path})"
    return "\n".join(sorted(p.name for p in target.iterdir()))
