"""glob, grep, ls (spec §6.6).

Read-only navigation. Permissions: only check 'read' on the resolved base
path (top of the pattern's prefix) — globbing 1000 files shouldn't issue
1000 permission checks.
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


def _virtual_prefix(path: str) -> tuple[str, str]:
    """Returns (prefix, remainder). Prefix is /project."""
    if path.startswith("/project"):
        return "/project", path[len("/project"):].lstrip("/")
    raise ValueError(f"unsupported virtual path: {path!r}")


def _real_root(ctx: ToolContext, prefix: str) -> Path:
    return ctx.project_root


def _iter_files(target: Path) -> Iterator[Path]:
    """Files under `target`, never descending into an excluded control/VCS dir
    (A3). `os.walk` with in-place `dirnames` pruning means the walk does not even
    *enumerate* inside `.imp/`/`.git/` — airtight and cheap on a pathological
    control dir with millions of entries (F7), and the multi-GB `.imp/events.jsonl`
    is never opened or stat'd."""
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(target):
        # Prune in place so os.walk skips these subtrees entirely.
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        base = Path(dirpath)
        for name in filenames:
            yield base / name


@imp_tool(
    name="glob",
    description=(
        "List file paths matching a virtual-prefixed glob (e.g. /project/**/*.py). "
        "Skips .imp/ and .git/."
    ),
)
async def glob(ctx: ToolContext, pattern: str) -> str:
    prefix, rel = _virtual_prefix(pattern)
    ctx.check_fs_permission("read", prefix + "/")
    root = _real_root(ctx, prefix)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    for p in sorted(root.glob(rel)):
        await tool_checkpoint(coordinator, deadline)
        rel_parts = p.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        out.append(f"{prefix}/{p.relative_to(root).as_posix()}")
    return "\n".join(out) if out else "(no matches)"


@imp_tool(
    name="grep",
    description=(
        "Search for a regex pattern in files under a virtual path. "
        "Returns matching lines with file paths. Skips .imp/ and .git/; "
        "output is capped at 50 KB."
    ),
)
async def grep(ctx: ToolContext, pattern: str, path: str = "/project") -> str:
    prefix, rel = _virtual_prefix(path)
    ctx.check_fs_permission("read", prefix + "/")
    root = _real_root(ctx, prefix)
    target = root / rel if rel else root
    rgx = re.compile(pattern)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    out_bytes = 0
    for f in _iter_files(target):
        # Per-file checkpoint: services Ctrl-C / the wall-clock deadline even
        # between files (B.1 / C-i). read_text_capped checkpoints each chunk too.
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
                entry = f"{prefix}/{rel_str}:{i}:{line}"
                out.append(entry)
                out_bytes += len(entry) + 1
                if out_bytes >= TOOL_OUTPUT_CAP_BYTES:
                    out.append(OUTPUT_TRUNCATION_MARKER.strip())
                    return "\n".join(out)
        if scan_truncated:
            # F3: read_text_capped stopped at the 5 MB scan ceiling — matches past
            # it are NOT seen. Emit a visible per-file note so the agent doesn't
            # read empty output as "definitely no match" (unlike the output cap,
            # the scan ceiling is otherwise silent).
            note = f"{prefix}/{rel_str}: [scanned first 5 MB only]"
            out.append(note)
            out_bytes += len(note) + 1
            if out_bytes >= TOOL_OUTPUT_CAP_BYTES:
                out.append(OUTPUT_TRUNCATION_MARKER.strip())
                return "\n".join(out)
    return "\n".join(out) if out else "(no matches)"


@imp_tool(name="ls", description="List directory contents at a virtual path.")
def ls(ctx: ToolContext, path: str) -> str:
    # NOTE: unlike grep/glob, ls does NOT exclude .imp/.git — it lists one level
    # of NAMES only (no recursion, no reads), so it cannot reproduce the runaway
    # and showing the control dirs as entries is intentional.
    prefix, rel = _virtual_prefix(path)
    ctx.check_fs_permission("read", path)
    root = _real_root(ctx, prefix)
    target = root / rel if rel else root
    if not target.exists():
        return f"(no such directory: {path})"
    return "\n".join(sorted(p.name for p in target.iterdir()))
