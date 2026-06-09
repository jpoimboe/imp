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
from typing import Literal

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import (
    EXCLUDED_DIRS,
    OUTPUT_TRUNCATION_MARKER,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    read_text_capped,
    tool_checkpoint,
)
from imp.agents.tools.lines import file_is_cap_truncated, split_lines, truncate_line


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
        # Sorted traversal (dirnames in-place so the walk itself is ordered):
        # files_with_matches + head_limit early exit make WHICH files the
        # agent sees depend on enumeration order — bless determinism, not the
        # filesystem's whim. Still prunes excluded control/VCS dirs (A3).
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        base = Path(dirpath)
        for name in sorted(filenames):
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
    # Reject patterns with `..` or an absolute prefix before the glob runs.
    # Wildcards (`*`/`**`) have no `..` component, so clean relative patterns pass.
    # Per-result p.resolve().is_relative_to(root) below is the second line of
    # defense for any result glob() produces via symlinks or dotdot traversal.
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


_GREP_MODES = ("files_with_matches", "content", "count")


@imp_tool(
    name="grep",
    description=(
        "Search for a regex pattern in files under a relative path (default '.'). "
        "Returns matching lines with file paths. Skips .imp/ and .git/; "
        "output is capped at 50 KB."
    ),
)
async def grep(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    *,
    output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
    case_insensitive: bool = False,
) -> str:
    # No validation layer sits between the JSON schema and this call — every
    # constraint is enforced here in the body.
    if output_mode not in _GREP_MODES:
        raise ValueError(f"invalid output_mode {output_mode!r}; "
                         f"valid modes: files_with_matches, content, count")
    ctx.check_fs_permission("read", path)
    target = ctx.resolve_path(path)
    # A typo'd/stale path — or a fifo/socket/device — must not read as
    # "searched, nothing found".
    if not (target.is_file() or target.is_dir()):
        raise FileNotFoundError(f"no such path: {path}")
    root = ctx.project_root.resolve()
    rgx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    out_bytes = 0
    capped = False      # the 50 KB backstop fired: stop scanning, marker appended
    result_rows = 0     # what the sentinel (and head_limit, Task 12) key on
    notes: list[str] = []  # trailing-mode scan notes (files_with_matches/count)

    def emit(row: str) -> None:
        nonlocal out_bytes, capped
        out.append(row)
        out_bytes += len(row) + 1  # +1 = the join newline
        if out_bytes >= TOOL_OUTPUT_CAP_BYTES:
            capped = True  # crossing row stays emitted (matches old behavior)

    for f in _iter_files(target):
        if capped:
            break
        await tool_checkpoint(coordinator, deadline)
        try:
            text, read_truncated = await read_text_capped(
                f, coordinator=coordinator, deadline=deadline)
        except OSError:
            continue
        cap_truncated = file_is_cap_truncated(read_truncated, f)
        rel_str = f.relative_to(root).as_posix()
        lines = split_lines(text, cap_truncated=cap_truncated)

        if output_mode == "files_with_matches":
            # Stop at the FIRST match — a regex-time win only (the bounded
            # read is already paid). A LISTED truncated file resolved itself;
            # an UNLISTED one is a potential false negative — note it.
            if any(rgx.search(line) for line in lines):
                emit(rel_str)
                result_rows += 1
            elif cap_truncated:
                notes.append(f"[{rel_str}: scanned first 5 MB only]")
        elif output_mode == "count":
            n = sum(1 for line in lines if rgx.search(line))
            if n:  # non-zero rows only (ripgrep -c, not GNU grep's path:0 flood)
                emit(f"{rel_str}:{n}")
                result_rows += 1
            if cap_truncated:  # the count is a lower bound — always note
                notes.append(f"[{rel_str}: scanned first 5 MB only]")
        else:  # content
            for i, line in enumerate(lines, start=1):
                if rgx.search(line):
                    emit(f"{rel_str}:{i}:{truncate_line(line)}")
                    result_rows += 1
                    if capped:
                        break
            if cap_truncated and not capped:
                # Inline (as today), own bracketed line in every mode.
                emit(f"[{rel_str}: scanned first 5 MB only]")

    if result_rows == 0:
        # Sentinel keys on zero RESULT rows, not on empty output — a note
        # line must never suppress it (agents pattern-match on the sentinel).
        out.insert(0, "(no matches)")
    if not capped:
        for note in notes:  # trailing modes: result rows, then notes
            if capped:
                break
            emit(note)
    if capped:
        # Appended AFTER accounting stops. Remaining notes (if any) were
        # dropped — acceptable only because this marker itself discloses
        # that output was physically cut.
        out.append(OUTPUT_TRUNCATION_MARKER.strip())
    return "\n".join(out)


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
