"""Background process execution (spec: 2026-06-09-background-execution-design).

`bash(run_in_background=True)` spawns a long-lived child with stdout/stderr
redirected to per-invocation scratch FILES (a long-lived child filling an OS
pipe with no reader deadlocks); `bash_output` tails them on per-stream
cursors; `bash_kill` terminates the group. Lifetime is the launching
invocation: teardown (`terminate_background_processes`) kills every group and
persists tombstones so a post-resume `bash_output("bg1")` gets an explanatory
result instead of ValueError.

No long-lived watcher task (hosting tasks across an async generator's yields
is the anyio scope-corruption hazard): `_sweep_bg` runs at points that
provably execute — every tool dispatch, every wait=True tick, a ~1 s ticker
inside foreground bash's per-call task group, and post-API. The stream-file
cap is therefore a KILL-POLICY trigger bounded by sweep cadence, not a disk
ceiling.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL
from typing import Any

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools import limits as _limits
from imp.agents.tools.context import ToolContext, kill_process_group
from imp.agents.tools.limits import (
    PROCESS_KILL_GRACE_S,
    TOOL_OUTPUT_CAP_BYTES,
    tool_checkpoint,
)

_CMD_TRUNC = 200      # command excerpt in warnings + tombstones (multi-KB sh -c
                      # scripts would otherwise render whole in the narrative)
_WAIT_POLL_S = 0.05   # wait=True inter-tick idle sleep (a REAL sleep — the
                      # checkpoint's sleep(0) is a scheduling yield, not pacing)
_ID_RE = re.compile(r"^bg(\d+)$")
_FILE_RE = re.compile(r"^bg(\d+)\.(?:out|err)$")


@dataclass
class BgProc:
    id: str
    proc: Any                       # anyio Process handle (pid/returncode/wait)
    out_path: Path
    err_path: Path
    command: str
    out_cursor: int = 0
    err_cursor: int = 0
    status: str = "running"         # running | exited | killed
    kill_reason: str | None = None  # interrupted | output-cap | agent
    returncode: int | None = None


def _bg_dir(ctx: ToolContext) -> Path:
    """<run>/bg/<sanitized agent_id>-<sha1[:8]>. The run dir
    (events_path.parent) is SHARED with subagents — namespacing by the
    per-invocation agent_id is what keeps a parent's bg1 and a subagent's bg1
    from resolving to the same path. The hash suffix disambiguates raw ids the
    many-to-one sanitizer collapses."""
    raw = ctx.agent_id or "unnamed"
    sane = re.sub(r"[^A-Za-z0-9]", "_", raw)
    suffix = hashlib.sha1(raw.encode()).hexdigest()[:8]
    return ctx.events_path.parent / "bg" / f"{sane}-{suffix}"


def _tombstone_path(ctx: ToolContext) -> Path:
    return _bg_dir(ctx) / "tombstones.json"


def _load_tombstones(path: Path) -> dict[str, dict[str, Any]]:
    """Tolerate a missing/torn/garbage file as empty — a torn write mid-crash
    is precisely the scenario the file exists to survive; a raise-forever
    tombstone file would poison every later teardown and lookup."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _seed_counter(ctx: ToolContext) -> None:
    """No id aliasing across resume: a resume re-drives with a fresh
    ToolContext (counter 0) but the same agent_id, and the resumed model's
    transcript still references the pre-pause bg1. Seed past
    max(tombstone-file ids, stream-FILENAME ids): filenames matter because
    after an ungraceful death the dir has stream files but NO tombstone file
    (teardown never ran)."""
    if ctx._bg_counter != 0:
        return
    d = _bg_dir(ctx)
    if not d.is_dir():
        return
    ids = [int(m.group(1)) for p in d.iterdir()
           if (m := _FILE_RE.match(p.name))]
    ids += [int(m.group(1)) for k in _load_tombstones(_tombstone_path(ctx))
            if (m := _ID_RE.match(k))]
    ctx._bg_counter = max(ids, default=0)


def _split_incomplete_utf8_tail(buf: bytes) -> tuple[bytes, bytes]:
    """(emit, holdback). Holds back ONLY a valid incomplete multibyte UTF-8
    prefix in the last 1-3 bytes (the next read re-reads it from the file via
    the unadvanced cursor — the cursor is the sole carrier, there is no carry
    buffer). Anything else — binary, a corrupt byte — is emitted so
    errors='replace' decoding advances the cursor fully (no livelock)."""
    n = len(buf)
    for back in range(1, min(3, n) + 1):
        b = buf[n - back]
        if b & 0xC0 == 0x80:
            continue                      # continuation byte — keep scanning
        if b & 0xE0 == 0xC0:
            need = 2
        elif b & 0xF0 == 0xE0:
            need = 3
        elif b & 0xF8 == 0xF0:
            need = 4
        else:
            return buf, b""               # ASCII or invalid lead byte
        if back < need and all(c & 0xC0 == 0x80 for c in buf[n - back + 1:]):
            return buf[: n - back], buf[n - back:]
        return buf, b""                   # complete (or overlong/garbage)
    return buf, b""                       # 3 bare continuations: corrupt — emit


async def spawn_background(
    ctx: ToolContext, *, command: str, argv: list[str], cwd: str,
    env: dict[str, str],
) -> str:
    """Spawn ordering (no orphan window): scratch dir + both write-side files
    BEFORE the spawn; register the BgProc immediately AFTER open_process
    returns; close the parent's fd copies LAST. No parent-side write handles
    are retained — the child writes through its own inherited fds; a retained
    parent handle would never be written through and its flush would flush
    nothing. Truncating open ('wb') is belt-and-braces: counter seeding means
    the same <id>.out is never legitimately reopened."""
    _seed_counter(ctx)
    # A pre-spawn/spawn failure CONSUMES this id (the next spawn is bg2 even
    # if bg1 never existed). Harmless — seeding tolerates gaps — and must NOT
    # be "fixed" with a decrement, which would re-open the aliasing window.
    bg_id = ctx.next_bg_id()
    d = _bg_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    out_path = d / f"{bg_id}.out"
    err_path = d / f"{bg_id}.err"
    kwargs: dict[str, object] = {}
    if hasattr(os, "setsid"):
        # Same session-leader invariant as foreground bash: pgid == pid, so
        # kill_process_group / os.killpg reaches the whole tree — including
        # forked children that survive the leader.
        kwargs["start_new_session"] = True
    out_f = open(out_path, "wb")  # noqa: SIM115
    try:
        err_f = open(err_path, "wb")  # noqa: SIM115
    except OSError:
        out_f.close()
        with contextlib.suppress(OSError):
            out_path.unlink()
        raise
    try:
        proc = await anyio.open_process(
            argv,
            # Pinned: an inherited stdin would let the child block on read,
            # steal terminal input, or hold a pipe open. DEVNULL = instant EOF.
            stdin=DEVNULL,
            stdout=out_f,
            stderr=err_f,
            cwd=cwd,
            env=env,
            **kwargs,  # type: ignore[arg-type]
        )
    except BaseException:
        out_f.close()
        err_f.close()
        with contextlib.suppress(OSError):
            out_path.unlink()
        with contextlib.suppress(OSError):
            err_path.unlink()
        raise
    bp = BgProc(id=bg_id, proc=proc, out_path=out_path, err_path=err_path,
                command=command)
    ctx.background_processes[bg_id] = bp
    out_f.close()
    err_f.close()
    root = ctx.project_root.resolve()
    return json.dumps({
        "id": bg_id,
        "pid": proc.pid,
        "command": command,
        "status": "running",
        # Worktree-root-relative: valid for bash's DEFAULT cwd (the worktree
        # root); a caller passing a non-root cwd must resolve them itself.
        "out_path": os.path.relpath(out_path.resolve(), root),
        "err_path": os.path.relpath(err_path.resolve(), root),
    }, ensure_ascii=False)
