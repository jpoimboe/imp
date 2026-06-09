"""glob, grep, ls (spec §6.6).

Read-only navigation over worktree-RELATIVE paths. Every tool funnels through
ToolContext.resolve_path for the normalize-then-contain escape guard (spec H1):
a `..`-escape or absolute-outside-root raises PermissionDenied. Reported paths
are relative to the worktree root.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import (
    EXCLUDED_DIRS,
    MATCH_LINE_CAP_BYTES,
    OUTPUT_TRUNCATION_MARKER,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    read_text_capped,
    tool_checkpoint,
)


def _iter_files(target: Path) -> Iterator[Path]:
    """Files under `target`, never descending into an excluded control/VCS dir
    (A3). `os.walk` with in-place `dirnames` pruning means the walk does not even
    *enumerate* inside `.imp/`/`.git/`."""
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        base = Path(dirpath)
        for name in filenames:
            yield base / name


@imp_tool(
    name="glob",
    description=(
        "List file paths matching a relative glob (e.g. **/*.py or src/**/*.ts). "
        "Skips .imp/ and .git/."
    ),
)
async def glob(ctx: ToolContext, pattern: str) -> str:
    ctx.check_fs_permission("read", pattern)
    root = ctx.project_root.resolve()
    # Validate containment of the pattern (rejects absolute + `..`); the magic
    # chars `*`/`**` are treated as literal segments by resolve() — they contain
    # no `..`, so a clean relative pattern passes (defense-in-depth, spec A.3.3).
    ctx.resolve_path(pattern)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    for p in sorted(root.glob(pattern)):
        await tool_checkpoint(coordinator, deadline)
        if not p.resolve().is_relative_to(root):
            continue                            # belt-and-suspenders vs symlinks
        rel_parts = p.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        out.append(p.relative_to(root).as_posix())
    return "\n".join(out) if out else "(no matches)"


@imp_tool(
    name="grep",
    description=(
        "Search for a regex pattern in files under a relative path (default '.'). "
        "Returns matching lines with file paths. Skips .imp/ and .git/; "
        "output is capped at 50 KB."
    ),
)
async def grep(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    ctx.check_fs_permission("read", path)
    target = ctx.resolve_path(path)
    root = ctx.project_root.resolve()
    rgx = re.compile(pattern)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    out_bytes = 0
    for f in _iter_files(target):
        await tool_checkpoint(coordinator, deadline)
        try:
            text, scan_truncated = await read_text_capped(
                f, coordinator=coordinator, deadline=deadline)
        except OSError:
            continue
        rel_str = f.relative_to(root).as_posix()
        for i, line in enumerate(text.splitlines(), start=1):
            if rgx.search(line):
                if len(line) > MATCH_LINE_CAP_BYTES:
                    line = line[:MATCH_LINE_CAP_BYTES] + "…[truncated]"
                entry = f"{rel_str}:{i}:{line}"
                out.append(entry)
                out_bytes += len(entry) + 1
                if out_bytes >= TOOL_OUTPUT_CAP_BYTES:
                    out.append(OUTPUT_TRUNCATION_MARKER.strip())
                    return "\n".join(out)
        if scan_truncated:
            note = f"{rel_str}: [scanned first 5 MB only]"
            out.append(note)
            out_bytes += len(note) + 1
            if out_bytes >= TOOL_OUTPUT_CAP_BYTES:
                out.append(OUTPUT_TRUNCATION_MARKER.strip())
                return "\n".join(out)
    return "\n".join(out) if out else "(no matches)"


@imp_tool(name="ls", description="List directory contents at a relative path.")
def ls(ctx: ToolContext, path: str) -> str:
    # NOTE: unlike grep/glob, ls does NOT exclude .imp/.git — it lists one level
    # of NAMES only (no recursion, no reads), so it cannot reproduce the runaway
    # and showing the control dirs as entries is intentional.
    ctx.check_fs_permission("read", path)
    target = ctx.resolve_path(path)
    if not target.exists():
        return f"(no such directory: {path})"
    return "\n".join(sorted(p.name for p in target.iterdir()))
