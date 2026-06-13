"""glob, grep (spec §6.6).

Read-only navigation. Both glob and grep route their search root through
ToolContext.authorize_fs(("read",), path) — the same gate the fs ops use —
so they reach exactly the paths read_file/write_file/edit_file can: in-worktree
paths via fs_permissions, out-of-worktree paths bounded by the sandbox mirror.
In-worktree matches are reported relative to the worktree root; out-of-worktree
matches as absolute paths.
"""

from __future__ import annotations

import fnmatch
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
            p = base / name
            # os.walk lists fifos/sockets/devices in filenames; opening a
            # fifo read-only blocks until a writer appears — in a worker
            # thread, before any tool_checkpoint and immune to cancellation —
            # wedging the whole call. Walk only regular files. (An EXPLICIT
            # non-regular path still errors loudly in grep — explicit path is
            # explicit intent.)
            if p.is_file():
                yield p


@imp_tool(
    name="glob",
    description=(
        "List file paths matching a relative glob (e.g. **/*.py or "
        "src/**/*.ts) under an optional `path` base (default '.'). Absolute "
        "and out-of-worktree bases are accepted, bounded by the sandbox like "
        "bash; in-worktree matches are reported relative to the root, external "
        "matches as absolute paths. Skips .imp/ and .git/. Uses pathlib glob "
        "semantics — a bare *.py matches top-level files only; this differs "
        "from grep's `glob` parameter, where * and ** cross /. The `pattern` "
        "stays relative; search elsewhere via `path`, not an absolute pattern."
    ),
)
async def glob(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    base = ctx.authorize_fs(("read",), path)
    root = ctx.project_root.resolve()
    inside = base.is_relative_to(root)
    policy = ctx.sandbox_policy
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    for p in sorted(base.glob(pattern)):
        await tool_checkpoint(coordinator, deadline)
        resolved = p.resolve()
        if inside:
            if not resolved.is_relative_to(root):
                continue                        # belt-and-suspenders vs symlinks
        elif policy is not None and policy.active and not policy.allows_read(resolved):
            continue                            # masked secret behind a symlink/in-tree
        if any(part in EXCLUDED_DIRS for part in p.relative_to(base).parts):
            continue
        out.append(p.relative_to(root).as_posix() if inside else p.as_posix())
    return "\n".join(out) if out else "(no matches)"


def _glob_variants(pat: str) -> list[str]:
    """Expand each `**/` occurrence (literal, non-overlapping, left-to-right —
    the same occurrences `pat.count("**/")` sees) as independently kept or
    deleted, restoring the zero-or-more-directories meaning fnmatch lacks
    (fnmatch's `**/` must consume at least one `/`, so `src/**/*.ts` alone
    would silently miss files directly inside src/). 2^n variants; the caller
    bounds n at 5. A single global replace("**/", "") would NOT suffice: it
    yields only the all-kept and all-deleted variants and misses mixed-depth
    cases anchored by a literal segment. Accepted corner: deleting a
    mid-segment `**/` (a**/b -> ab) licenses a match no glob dialect accepts —
    false INCLUDES on pathological patterns only."""
    variants = [""]
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            variants = [v + tail for v in variants for tail in ("**/", "")]
            i += 3
        else:
            variants = [v + pat[i] for v in variants]
            i += 1
    return list(dict.fromkeys(variants))


def _compile_glob(pat: str) -> re.Pattern[str]:
    """ONE compiled union regex per grep call — never per candidate file.
    fnmatch.translate gives fnmatchCASE semantics (no os.path.normcase):
    case-sensitive, platform-independent, `*`/`**` cross `/`. Matched against
    the worktree-relative POSIX path — NOT relative to grep's `path` arg."""
    return re.compile("|".join(
        "(?:" + fnmatch.translate(v) + ")" for v in _glob_variants(pat)))


_GREP_MODES = ("files_with_matches", "content", "count")


@imp_tool(
    name="grep",
    description=(
        "Search file contents with a regex under a relative path (default "
        "'.'). output_mode: 'files_with_matches' (default) lists matching "
        "file paths only; 'content' shows path:lineno:line rows (context "
        "rows from before/after/context use path-lineno-line); 'count' shows "
        "path:count for files with matches. before/after/context require "
        "content mode. head_limit caps result rows. case_insensitive applies "
        "to the REGEX only — the `glob` filter is always case-sensitive. "
        "`glob` filters by the path relative to the project root (NOT "
        "relative to the `path` argument — use glob=\"src/a/*.py\" or a "
        "wildcard basename pattern like *.py), with fnmatch semantics where "
        "both * and ** cross "
        "/ — unlike the glob tool, whose patterns do not. Lines are split on "
        "\\n only: on CRLF files match the invisible trailing \\r with "
        "`\\r?$`. Output rows can carry display artifacts that are not file "
        "content (`…[truncated]` on >2000-char lines, U+FFFD for invalid "
        "bytes) plus the invisible trailing \\r that IS content on CRLF "
        "lines — never paste rows into edit_file/write_file. "
        "Skips .imp/ and .git/; scans the first 5 MB per file; output capped "
        "at 50 KB."
    ),
)
async def grep(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    *,
    output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
    case_insensitive: bool = False,
    glob: str | None = None,
    before: int = 0,
    after: int = 0,
    context: int = 0,
    head_limit: int | None = None,
) -> str:
    # No validation layer sits between the JSON schema and this call — every
    # constraint is enforced here in the body.
    if output_mode not in _GREP_MODES:
        raise ValueError(f"invalid output_mode {output_mode!r}; "
                         f"valid modes: files_with_matches, content, count")
    # `glob` (the wire name agents expect) shadows this module's glob() tool
    # function inside this body — rebind to a distinct local and never call
    # glob() in here.
    glob_pattern = glob
    glob_rx: re.Pattern[str] | None = None
    if glob_pattern is not None:
        if not glob_pattern.strip():
            raise ValueError("glob pattern must not be empty")
        if glob_pattern.count("**/") > 5:
            raise ValueError("glob pattern has too many **/ occurrences (max 5)")
        glob_rx = _compile_glob(glob_pattern)
    if head_limit is not None and head_limit < 1:
        raise ValueError("head_limit must be >= 1")
    if before < 0 or after < 0 or context < 0:
        raise ValueError("before/after/context must be >= 0")
    if (before or after or context) and output_mode != "content":
        # Pinned to NONZERO: async_invoke calls fn(ctx, **args), so the body
        # cannot distinguish an explicit before=0 from an omitted one.
        raise ValueError("context/before/after require output_mode='content'")
    # Order-free precedence (a JSON call has no flag order): per-side
    # effective context is max(side, context).
    eff_before = max(before, context)
    eff_after = max(after, context)
    target = ctx.authorize_fs(("read",), path)
    # A typo'd/stale path — or a fifo/socket/device — must not read as
    # "searched, nothing found".
    if not (target.is_file() or target.is_dir()):
        raise FileNotFoundError(f"no such path: {path}")
    root = ctx.project_root.resolve()
    inside = target.is_relative_to(root)
    rgx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    out: list[str] = []
    out_bytes = 0
    capped = False      # the 50 KB backstop fired: stop scanning, marker appended
    limit_hit = False   # head_limit reached: stop scanning, notice appended
    result_rows = 0     # what the sentinel (and head_limit, Task 12) key on
    notes: list[str] = []  # trailing-mode scan notes (files_with_matches/count)
    emitted_a_block = False  # tracks whether any context block was emitted (for -- separators)

    def emit(row: str) -> bool:
        """Append a row, charging it to the 50 KB budget. Returns True once
        the budget has fired (the crossing row stays emitted — old behavior).
        Returned rather than only closed-over: mypy narrows a closure-mutated
        bool to Literal[False] at the call sites and flags the checks
        unreachable."""
        nonlocal out_bytes, capped
        out.append(row)
        out_bytes += len(row) + 1  # +1 = the join newline
        if out_bytes >= TOOL_OUTPUT_CAP_BYTES:
            capped = True
        return capped

    for f in _iter_files(target):
        if capped or limit_hit:
            break
        await tool_checkpoint(coordinator, deadline)
        disp = f.relative_to(root).as_posix() if inside else f.as_posix()
        if glob_rx is not None and glob_rx.match(disp) is None:
            continue  # applies uniformly — including when path names one file
        try:
            text, read_truncated = await read_text_capped(
                f, coordinator=coordinator, deadline=deadline)
        except OSError:
            continue
        cap_truncated = file_is_cap_truncated(read_truncated, f)
        lines = split_lines(text, cap_truncated=cap_truncated)

        if output_mode == "files_with_matches":
            # Stop at the FIRST match — a regex-time win only (the bounded
            # read is already paid). A LISTED truncated file resolved itself;
            # an UNLISTED one is a potential false negative — note it.
            if any(rgx.search(line) for line in lines):
                capped = emit(disp)
                result_rows += 1
                if head_limit is not None and result_rows >= head_limit:
                    limit_hit = True  # the row IS the block: stop scanning
            elif cap_truncated:
                notes.append(f"[{disp}: scanned first 5 MB only]")
        elif output_mode == "count":
            n = sum(1 for line in lines if rgx.search(line))
            if n:  # non-zero rows only (ripgrep -c, not GNU grep's path:0 flood)
                capped = emit(f"{disp}:{n}")
                result_rows += 1
                if head_limit is not None and result_rows >= head_limit:
                    limit_hit = True
            if cap_truncated:  # the count is a lower bound — always note
                notes.append(f"[{disp}: scanned first 5 MB only]")
        else:  # content
            match_idxs = [i for i, line in enumerate(lines, start=1)
                          if rgx.search(line)]
            if eff_before == 0 and eff_after == 0:
                # Zero-context: the bare row stream. Every match is its own
                # "block" but NO -- separators (GNU grep emits group
                # separators only when context is active).
                for m in match_idxs:
                    if head_limit is not None and result_rows >= head_limit:
                        limit_hit = True
                        break
                    capped = emit(f"{disp}:{m}:{truncate_line(lines[m - 1])}")
                    result_rows += 1
                    if capped:
                        break
            else:
                match_set = set(match_idxs)
                # Merge overlapping/adjacent ranges into one block (grep
                # behavior — no -- between them). Block = [start, end, n_matches].
                blocks: list[list[int]] = []
                for m in match_idxs:
                    lo = max(1, m - eff_before)
                    hi = min(len(lines), m + eff_after)
                    if blocks and lo <= blocks[-1][1] + 1:
                        blocks[-1][1] = max(blocks[-1][1], hi)
                        blocks[-1][2] += 1
                    else:
                        blocks.append([lo, hi, 1])
                for lo, hi, n_matches in blocks:
                    if head_limit is not None and result_rows >= head_limit:
                        limit_hit = True  # never cut a block — stop before it
                        break
                    if emitted_a_block:
                        # Distinct blocks get a -- separator — including
                        # across file boundaries (blocks never merge across
                        # files; the row prefixes differ by construction).
                        capped = emit("--")
                        if capped:
                            # Cap landed ON the separator: the separator is
                            # the crossing row that stays; the next block's
                            # rows must not leak past the budget (every other
                            # emit site checks immediately).
                            break
                    emitted_a_block = True
                    for ln in range(lo, hi + 1):
                        sep = ":" if ln in match_set else "-"
                        # Context rows share the match rows' per-line cap —
                        # an uncapped 2 MB neighbor would eat the whole
                        # 50 KB budget as one row.
                        capped = emit(f"{disp}{sep}{ln}{sep}{truncate_line(lines[ln - 1])}")
                        if capped:
                            break
                    # result_rows counts matches whose block BEGAN emitting
                    # (a mid-block byte-cap doesn't subtract) — deliberate:
                    # only the ==0 sentinel and the between-blocks head_limit
                    # consult it.
                    result_rows += n_matches
                    if capped:
                        break
            if cap_truncated and not capped:
                # Inline (as today), own bracketed line in every mode.
                capped = emit(f"[{disp}: scanned first 5 MB only]")
            if head_limit is not None and result_rows >= head_limit:
                limit_hit = True  # end-of-file boundary: the row checks above
                # only run for files WITH matches — without this, a limit
                # landing exactly on a file's last match keeps scanning every
                # remaining file and never emits the notice.

    if result_rows == 0:
        # Sentinel keys on zero RESULT rows, not on empty output — a note
        # line must never suppress it (agents pattern-match on the sentinel).
        out.insert(0, "(no matches)")
    if not capped:
        for note in notes:  # trailing modes: result rows, then notes
            if capped:
                break
            capped = emit(note)
    if capped:
        # Appended AFTER accounting stops. Remaining notes (if any) were
        # dropped — acceptable only because this marker itself discloses
        # that output was physically cut.
        out.append(OUTPUT_TRUNCATION_MARKER.strip())
    elif limit_hit:
        # Distinct from the byte-cap marker (different agent recovery), and
        # uncounted against the budget. Early exit means no omitted COUNT is
        # known (the whole point is not scanning further) — and the notice
        # may be vacuous when the limit lands exactly on the final result;
        # computing that would require the very scan the cap avoids.
        out.append(f"[further results omitted: head_limit={head_limit} reached "
                   f"— raise head_limit or narrow the pattern]")
    return "\n".join(out)
