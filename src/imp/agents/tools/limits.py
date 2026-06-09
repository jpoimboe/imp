"""Bounded-output / cooperative-cancel primitives shared by the tool layer
(spec: runaway-and-interrupt hardening).

This module imports ONLY stdlib + anyio — never anything under `imp` — so both
the tools (`nav`, `fs`, `bash`, `context`) and the engine drive loop can import
it without forming a cycle. It is the single home for every tunable the
hardening introduces.
"""

from __future__ import annotations

from pathlib import Path

import anyio

# --- Output / read bounds ---------------------------------------------------
TOOL_OUTPUT_CAP_BYTES = 50_000           # head-only cap on what the LLM sees
FILE_READ_CAP_BYTES = 5_000_000          # per-file ceiling for the bounded reader
MATCH_LINE_CAP_BYTES = 2_000             # byte-truncate each matched grep line
READ_CHUNK_BYTES = 65_536                # cooperative read granularity
# F4: the "50 KB" text is bound to TOOL_OUTPUT_CAP_BYTES above — if you change the
# cap, change this label (and the tests that assert the literal) together. The
# same binding holds for the "5 MB"/"first 5 MB" literals in the read_file/grep/
# edit_file notice skeletons (fs.py, nav.py): they are FILE_READ_CAP_BYTES in
# prose, and tests assert them verbatim — change the constant => change those
# skeletons and their tests in lockstep.
OUTPUT_TRUNCATION_MARKER = "\n... [output truncated at 50 KB] ..."

# --- Watchdog / teardown timing (distinct from HEADLESS_RETRY_CEILING_SECONDS,
# backoff.py:17, which is also 1800s but a cumulative-backoff cap) -----------
TOOL_WALLCLOCK_TIMEOUT_S = 1800.0        # B.1: per-tool-call deadline (30 min)
AGENT_INACTIVITY_TIMEOUT_S = 1800.0      # B.2: per-agent no-events window (30 min)
PROCESS_KILL_GRACE_S = 4.0               # C-ii: SIGTERM -> wait -> SIGKILL grace

# --- Control-dir + VCS exclusion (structured nav tools only) ----------------
EXCLUDED_DIRS = frozenset({".imp", ".git"})


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
