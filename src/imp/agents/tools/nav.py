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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Literal

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.ignore import (
    is_ignored,
    list_unignored,
    partition_ignored,
    repo_root,
)
from imp.agents.tools.limits import (
    ARTIFACTS_REL,
    EXCLUDED_DIRS,
    EXTERNAL_WALK_FILE_CAP,
    OUTPUT_TRUNCATION_MARKER,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    leads_to_or_within_artifacts,
    read_text_capped,
    tool_checkpoint,
    within_artifacts,
)
from imp.agents.tools.lines import file_is_cap_truncated, split_lines, truncate_line


def _iter_files(target: Path, policy: Any = None,
                root: Path | None = None,
                excluded: frozenset[str] = EXCLUDED_DIRS) -> Iterator[Path]:
    """Files under `target`, never descending into an excluded control/VCS dir
    (A3) — EXCEPT the per-change artifact subtree .imp/change/artifacts/, which is
    surfaced (passed via `root`, the worktree root, so the carve-out is anchored
    there; `root=None` for external targets keeps the old exclude-everything
    behavior). `os.walk` with in-place `dirnames` pruning means the walk does not
    even *enumerate* inside `.imp/`/`.git/` beyond the carve-out. When `policy` is
    an active SandboxPolicy, additionally prune directories and drop files whose
    resolved path fails `policy.allows_read` — the in-process mirror of the bwrap
    secret masks, so the search tool never out-reads `bash`-under-bwrap (a symlink
    into a masked dir is dropped by resolving before the check)."""
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return
    masking = policy is not None and policy.active
    for dirpath, dirnames, filenames in os.walk(target):
        # Sorted traversal (dirnames in-place so the walk itself is ordered):
        # files_with_matches + head_limit early exit make WHICH files the
        # agent sees depend on enumeration order — bless determinism, not the
        # filesystem's whim. Prunes excluded control/VCS dirs (A3) and, under
        # an active policy, secret-masked dirs (pruned in place: never enumerated
        # inside).
        keep: list[str] = []
        for d in sorted(dirnames):
            if d in excluded:
                child = Path(dirpath) / d
                if not (root is not None and child.is_relative_to(root)
                        and leads_to_or_within_artifacts(child.relative_to(root))):
                    continue
            keep.append(d)
        if masking:
            keep = [d for d in keep
                    if policy.allows_read((Path(dirpath) / d).resolve())]
        dirnames[:] = keep
        base = Path(dirpath)
        for name in sorted(filenames):
            p = base / name
            # os.walk lists fifos/sockets/devices in filenames; opening a
            # fifo read-only blocks until a writer appears — in a worker
            # thread, before any tool_checkpoint and immune to cancellation —
            # wedging the whole call. Walk only regular files. (An EXPLICIT
            # non-regular path still errors loudly in grep — explicit path is
            # explicit intent.)
            if not p.is_file():
                continue
            # A file directly under an excluded dir (e.g. .imp/events.jsonl) stays
            # hidden even though the walk descended .imp to reach the artifact subtree.
            if root is not None and p.is_relative_to(root):
                rel = p.relative_to(root)
                if (any(part in excluded for part in rel.parts)
                        and not within_artifacts(rel)):
                    continue
            if masking and not policy.allows_read(p.resolve()):
                continue
            yield p


def _git_files(unignored: set[Path], target: Path, root: Path,
               policy: Any) -> list[Path]:
    """Normal in-worktree git path: the unignored set UNION the
    .imp/change/artifacts/ carve-out portion under `target` (walked separately
    since .imp is gitignored), restricted to regular files under `target`,
    sandbox-masked, sorted (head_limit early-exit makes order observable)."""
    masking = policy is not None and policy.active
    candidates = set(unignored)
    art = root / ARTIFACTS_REL
    if art.is_dir() and (art.is_relative_to(target) or target.is_relative_to(art)):
        for dirpath, _dirnames, filenames in os.walk(art):
            base = Path(dirpath)
            for name in filenames:
                candidates.add(base / name)
    files: list[Path] = []
    for p in sorted(candidates):
        if not p.is_relative_to(target):
            continue
        if not p.is_file():
            continue
        if masking and not policy.allows_read(p.resolve()):
            continue
        files.append(p)
    return files


async def _grep_file_source(target: Path, root: Path, inside: bool,
                            policy: Any) -> Iterable[Path]:
    """Choose grep's file enumeration per the decision tree. External,
    file-target, no-repo, and escape-hatch cases return lazy iterators
    (preserving the external walk cap and prompt pause checkpoints); the normal
    in-worktree git case returns an eager, sorted list."""
    if not inside or target.is_file():
        return _iter_files(target, policy, root if inside else None)
    gtarget = target.resolve()
    repo = await repo_root(gtarget)
    if repo is None:
        return _iter_files(target, policy, root)
    if await is_ignored(repo, gtarget):
        return _iter_files(target, policy, root, excluded=frozenset({".git"}))
    unignored = await list_unignored(repo, gtarget)
    if unignored is None:
        return _iter_files(target, policy, root)
    return _git_files(unignored, gtarget, root, policy)


@imp_tool(
    name="glob",
    description=(
        "List file paths matching a relative glob (e.g. **/*.py or "
        "src/**/*.ts) under an optional `path` base (default '.'). Absolute "
        "and out-of-worktree bases are accepted, bounded by the sandbox like "
        "bash; in-worktree matches are reported relative to the root, external "
        "matches as absolute paths. Skips paths git ignores (build artifacts, "
        "caches, .venv, etc.) and the .git/ dir; the change's "
        ".imp/change/artifacts/ subtree stays visible, and explicitly targeting "
        "an ignored path searches it anyway. Uses pathlib glob "
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
    masking = policy is not None and policy.active
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S

    matched: list[Path] = []
    for p in sorted(base.glob(pattern)):
        await tool_checkpoint(coordinator, deadline)
        resolved = p.resolve()
        if inside:
            if not resolved.is_relative_to(root):
                continue                        # belt-and-suspenders vs symlinks
            if masking and not policy.allows_read(resolved):
                continue
        elif masking and not policy.allows_read(resolved):
            continue                            # masked secret behind a symlink/in-tree
        if (any(part in EXCLUDED_DIRS for part in p.relative_to(base).parts)
                and not (inside and within_artifacts(resolved.relative_to(root)))):
            continue
        matched.append(p)

    if inside and matched:
        gbase = base.resolve()
        repo = await repo_root(gbase if gbase.is_dir() else gbase.parent)
        if repo is not None and not await is_ignored(repo, gbase):
            to_check = {p.resolve() for p in matched
                        if not within_artifacts(p.resolve().relative_to(root))}
            ignored = await partition_ignored(repo, to_check)
            if ignored:
                matched = [p for p in matched if p.resolve() not in ignored]

    out = [p.relative_to(root).as_posix() if inside else p.as_posix()
           for p in matched]
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
    the worktree-relative POSIX path for in-worktree targets, or the absolute
    path for external targets — NOT relative to grep's `path` arg."""
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
        "`glob` filters by the worktree-relative path for in-worktree targets, "
        "or the absolute path for external targets (NOT "
        "relative to the `path` argument — use glob=\"src/a/*.py\" or a "
        "wildcard basename pattern like *.py), with fnmatch semantics where "
        "both * and ** cross "
        "/ — unlike the glob tool, whose patterns do not. Lines are split on "
        "\\n only: on CRLF files match the invisible trailing \\r with "
        "`\\r?$`. Output rows can carry display artifacts that are not file "
        "content (`…[truncated]` on >2000-char lines, U+FFFD for invalid "
        "bytes) plus the invisible trailing \\r that IS content on CRLF "
        "lines — never paste rows into edit_file/write_file. "
        "Skips paths git ignores (build artifacts, caches, .venv, etc.), the "
        ".git/ dir, and binary files (like ripgrep); the change's "
        ".imp/change/artifacts/ subtree stays visible, and explicitly targeting "
        "an ignored path searches it anyway. "
        "Scans the first 5 MB per file; output capped "
        "at 50 KB. Absolute and out-of-worktree paths are accepted, bounded by "
        "the sandbox like bash (in-worktree matches reported relative, external "
        "matches as absolute paths); an external-directory search stops after a "
        "fixed file-count cap, disclosed in the output."
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
    external = not inside
    policy = ctx.sandbox_policy
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

    file_source = await _grep_file_source(target, root, inside, policy)
    files_scanned = 0
    walk_capped = False
    for f in file_source:
        if capped or limit_hit:
            break
        if external and files_scanned >= EXTERNAL_WALK_FILE_CAP:
            walk_capped = True
            break
        files_scanned += 1
        await tool_checkpoint(coordinator, deadline)
        disp = f.relative_to(root).as_posix() if inside else f.as_posix()
        if glob_rx is not None and glob_rx.match(disp) is None:
            continue  # applies uniformly — including when path names one file
        try:
            text, read_truncated = await read_text_capped(
                f, coordinator=coordinator, deadline=deadline)
        except OSError:
            continue
        if "\x00" in text:
            continue  # binary file (NUL): skip silently, like ripgrep's default
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

    if result_rows == 0 and not walk_capped:
        # Sentinel keys on zero RESULT rows, not on empty output — a note
        # line must never suppress it (agents pattern-match on the sentinel).
        # Suppressed when walk_capped: the truncated walk may have matches in
        # unscanned files, so "(no matches)" would be a false certainty.
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
    if walk_capped:
        out.append(
            f"[external walk truncated: scanned first {EXTERNAL_WALK_FILE_CAP} "
            f"files — narrow the search path]"
        )
    return "\n".join(out)
