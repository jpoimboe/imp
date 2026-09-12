# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Bounded-output / cooperative-cancel primitives shared by the tool layer
(spec: runaway-and-interrupt hardening).

This module imports ONLY stdlib + anyio — never anything under `imp` — so both
the tools (`nav`, `fs`, `bash`, `context`) and the engine drive loop can import
it without forming a cycle. The watchdog/teardown timing constants the engine
shares live one level up in `imp.agents.timeouts`, which is stdlib-only and NOT
inside the `imp.agents.tools` package (whose `__init__` costs pydantic).
"""

from __future__ import annotations

from pathlib import Path

import anyio

# --- Output / read bounds ---------------------------------------------------
TOOL_OUTPUT_CAP_BYTES = 50_000           # head-only cap on what the LLM sees
FILE_READ_CAP_BYTES = 5_000_000          # per-file ceiling for the bounded reader
# per-line content cap: grep/read_file rows and bash stdout/stderr
MATCH_LINE_CAP_BYTES = 2_000
PREVIEW_BYTES = 2_000                    # spill-to-disk preview head (spill.py)
READ_CHUNK_BYTES = 65_536                # cooperative read granularity
# F4: the "50 KB" text is bound to TOOL_OUTPUT_CAP_BYTES above — if you change the
# cap, change this label (and the tests that assert the literal) together. The
# same binding holds for the "5 MB"/"first 5 MB" literals in the read_file/grep/
# edit_file notice skeletons (fs.py, nav.py): they are FILE_READ_CAP_BYTES in
# prose, and tests assert them verbatim — change the constant => change those
# skeletons and their tests in lockstep.
# bash's notice clauses carry "2000" and "30 KB" as prose literals bound to
# MATCH_LINE_CAP_BYTES and BASH_OUTPUT_CAP_BYTES (_clip_cap_notice_clause in
# bash.py), as does the imp-tool-reference skill's bash bullet, which states the
# cap's SHAPE and not just its number ("the two streams are cut to a combined
# 30 KB head"): change either constant => change those clauses, that bullet, and
# the tests that assert them verbatim (tests/test_bash_tool.py,
# tests/test_tool_reference_skill.py) in lockstep.
OUTPUT_TRUNCATION_MARKER = "\n... [output truncated at 50 KB] ..."

# Bash-tool-specific inline output cap. Deliberately SMALLER than the shared
# TOOL_OUTPUT_CAP_BYTES (50 KB) that read_file/grep display with: a raw shell
# command is the likeliest flooder, so its inline head is tightened while the
# full output still spills to a recoverable file. NOT an alias of the shared cap.
BASH_OUTPUT_CAP_BYTES = 30_000

# Subagent-RESULT spill trigger. NOT an alias of the equal-valued
# BASH_OUTPUT_CAP_BYTES: one bounds a child agent's report, the other shell output.
TASK_RESULT_CAP_BYTES = 30_000

# grep ONLY: an external-target walk halts after this many files SCANNED (not
# matched) and discloses a truncation notice. A performance brake (bounds work),
# not a security one — applies whenever the target is external, sandbox or not.
# In-worktree walks never observe it. glob is cap-exempt (it does not use
# _iter_files); the 30-min wall-clock deadline is the ultimate backstop for both.
EXTERNAL_WALK_FILE_CAP = 20_000

# --- Watchdog / teardown timing (a per-tool-call deadline, distinct from the
# per-category retry-backoff budgets in error_policy.py) ---------------------
TOOL_WALLCLOCK_TIMEOUT_S = 1800.0        # B.1: per-tool-call deadline (30 min)
# Bash foreground deadline: a raw recursive shell walk is the runaway hazard, so
# a foreground bash command is reaped in minutes, not the 30-min fs/nav ceiling.
# A caller may pass a larger `timeout` (positive seconds) up to the MAX cap; the
# in-process fs/nav/symbol_outline tools keep TOOL_WALLCLOCK_TIMEOUT_S untouched.
BASH_FOREGROUND_TIMEOUT_S = 120.0        # default foreground bash deadline (2 min)
BASH_FOREGROUND_TIMEOUT_MAX_S = 600.0    # cap on a caller-supplied timeout (10 min)

# --- Background execution (bg.py) -------------------------------------------
# Per-stream FILE-SIZE ceiling for background process output. A breach is a
# KILL-POLICY TRIGGER (the sweep kills the process group), NOT a disk ceiling:
# worst-case disk = write rate x longest inter-sweep gap (the model-API call).
# Deliberately NOT aliased to FILE_READ_CAP_BYTES — that is a *read* bound;
# coupling them would let a future read-cap bump silently change kill policy.
BG_STREAM_FILE_CAP_BYTES = 5_000_000
# B.2 is INDEPENDENT of this budget: a bash_output(wait=True) runs inside a
# bracketed foreground tool call, so B.2 is suspended for the whole wait exactly
# like any other tool. The wait budget no longer has to fit under B.2 (it is now
# larger than the short B.2 window). tests/test_tool_limits.py pins the decoupling.
BG_WAIT_MAX_S = 1200.0                   # 20 min wait budget per bash_output call

# --- Control-dir + VCS exclusion (structured nav tools only) ----------------
EXCLUDED_DIRS = frozenset({".imp", ".git"})

# The quest-home subdirs the nav carve-out surfaces through that exclusion.
VISIBLE_QUEST_SUBDIRS = frozenset({"artifacts", "spike"})


def within_visible_quest_subtree(rel: Path) -> bool:
    """True when `rel` (worktree-relative) IS one of a quest's visible subtrees or
    sits inside it: .imp/quests/<id>/{artifacts,spike}[/...]."""
    parts = rel.parts
    return (len(parts) >= 4 and parts[0] == ".imp"
            and parts[1] == "quests" and parts[3] in VISIBLE_QUEST_SUBDIRS)


def leads_to_or_within_visible_quest_subtree(rel: Path) -> bool:
    """`within_visible_quest_subtree` plus the ancestor dirs (.imp, .imp/quests,
    .imp/quests/<id>) that lead down to a quest's visible subtrees."""
    parts = rel.parts
    if parts in {(".imp",), (".imp", "quests")}:
        return True
    if len(parts) == 3 and parts[0] == ".imp" and parts[1] == "quests":
        return True
    return within_visible_quest_subtree(rel)


class ToolAborted(Exception):
    """A cooperatively-cancellable in-process tool aborted mid-run.

    reason is 'interrupted' (a coordinator pause / Ctrl-C reached the checkpoint)
    or 'timed-out' (the B.1 wall-clock deadline passed). The provider turns the
    raised exception into an is_error tool_result the agent can react to.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def tool_checkpoint(coordinator: object | None, deadline: float | None) -> None:
    """One cooperative-cancel checkpoint for in-process tools.

    `await anyio.sleep(0)` FIRST so the ready signal-receiver task gets scheduled
    (a bare flag-check in a sync function has no yield point and cannot service
    Ctrl-C — that is the whole incident). Then abort if a pause was requested or
    the deadline passed. Called once per read chunk and once per scanned file.
    """
    await anyio.sleep(0)
    if coordinator is not None and getattr(coordinator, "should_pause", False):
        raise ToolAborted("interrupted")
    if deadline is not None and anyio.current_time() >= deadline:
        raise ToolAborted("timed-out")


async def read_bytes_capped(
    path: Path,
    *,
    coordinator: object | None = None,
    deadline: float | None = None,
    cap_bytes: int = FILE_READ_CAP_BYTES,
) -> tuple[bytes, bool]:
    """Read up to `cap_bytes` RAW BYTES from `path`, cooperatively. Returns
    (data, truncated). The byte ceiling is enforced DURING the read — never
    buffer past it (the incident's 3 GB single-line shape). Checkpoints each
    chunk so a pause / deadline preempts a huge file.

    NOTE: a file whose size is EXACTLY cap_bytes returns truncated=True even
    though nothing was dropped — the loop fills before a final read sees EOF.
    Conservative by design; callers disambiguate with st_size when it matters
    (see lines.file_is_cap_truncated).
    """
    buf = bytearray()
    async with await anyio.open_file(path, "rb") as f:
        while len(buf) < cap_bytes:
            await tool_checkpoint(coordinator, deadline)
            chunk = await f.read(min(READ_CHUNK_BYTES, cap_bytes - len(buf)))
            if not chunk:
                return bytes(buf), False
            buf.extend(chunk)
    return bytes(buf), True


async def read_text_capped(
    path: Path,
    *,
    coordinator: object | None = None,
    deadline: float | None = None,
    cap_bytes: int = FILE_READ_CAP_BYTES,
) -> tuple[str, bool]:
    """`read_bytes_capped`, decoded UTF-8 with errors="replace" — for
    DISPLAY-ONLY tools (read_file, grep). A read-modify-WRITE tool must use
    read_bytes_capped + strict decode instead: errors="replace" would bake
    U+FFFD into a write-back, permanently destroying non-UTF-8 bytes."""
    data, truncated = await read_bytes_capped(
        path, coordinator=coordinator, deadline=deadline, cap_bytes=cap_bytes)
    return data.decode("utf-8", "replace"), truncated
