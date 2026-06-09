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


def _reap(bp: BgProc) -> None:
    """Resolve an observed exit. SYNCHRONOUS check-and-set: flip to terminal
    before any side effect, NO await between check and set — callers (sweep,
    wait tick, snapshot poll, bash_kill, teardown) run in different tasks
    interleaved at await points; sequential idempotency is not enough."""
    rc = bp.proc.returncode
    if rc is None or bp.status != "running":
        return
    bp.status = "exited"
    bp.returncode = rc


def _read_page(bp: BgProc, which: str) -> tuple[str, int]:
    """One page from a stream file at its cursor -> (text, remaining_bytes).

    The cursor advances only by bytes actually returned, so a flood is PAGED
    across calls, never lost. UTF-8 rule (after EVERY page, cap-bounded or
    EOF-bounded): a valid incomplete multibyte prefix in the last 1-3 bytes is
    excluded and the cursor not advanced past it — re-read next call. Terminal
    flush: once status is terminal AND the read hit EOF, a held-back prefix
    can never complete — decode with replace and advance fully."""
    path = bp.out_path if which == "out" else bp.err_path
    cursor = bp.out_cursor if which == "out" else bp.err_cursor
    with open(path, "rb") as f:
        f.seek(cursor)
        chunk = f.read(TOOL_OUTPUT_CAP_BYTES)
        at_eof = len(chunk) < TOOL_OUTPUT_CAP_BYTES or f.read(1) == b""
        size = f.seek(0, os.SEEK_END)
    emit, held = _split_incomplete_utf8_tail(chunk)
    if held and bp.status != "running" and at_eof:
        emit, held = chunk, b""
    new_cursor = cursor + len(emit)
    if which == "out":
        bp.out_cursor = new_cursor
    else:
        bp.err_cursor = new_cursor
    return emit.decode("utf-8", "replace"), max(0, size - new_cursor)


def _snapshot(ctx: ToolContext, bp: BgProc, *, wait_ended: str | None = None) -> str:
    out_text, out_rem = _read_page(bp, "out")
    err_text, err_rem = _read_page(bp, "err")
    result: dict[str, Any] = {
        "stdout": out_text, "stderr": err_text,
        "status": bp.status, "returncode": bp.returncode,
    }
    if bp.kill_reason is not None:
        result["kill_reason"] = bp.kill_reason
    if wait_ended is not None:
        result["wait_ended"] = wait_ended
    if out_rem or err_rem:
        root = ctx.project_root.resolve()
        result["stdout_remaining_bytes"] = out_rem
        result["stderr_remaining_bytes"] = err_rem
        result["out_path"] = os.path.relpath(bp.out_path.resolve(), root)
        result["err_path"] = os.path.relpath(bp.err_path.resolve(), root)
        result["notice"] = (
            f'more output available — call bash_output("{bp.id}") again to '
            "continue paging, or process the stream files in bulk with bash "
            "(grep/tail)")
    # ensure_ascii=False: default JSON escaping inflates non-ASCII up to 6x
    # (a 50 KB CJK page becomes ~300 KB of context). Encoding, not semantics.
    return json.dumps(result, ensure_ascii=False)


def _snapshot_or_tombstone(
    ctx: ToolContext, bp: BgProc, *, wait_ended: str | None = None,
) -> str:
    try:
        return _snapshot(ctx, bp, wait_ended=wait_ended)
    except FileNotFoundError:
        # Teardown deleted the stream files mid-call. Its check-and-set pass
        # ran FIRST (ordering pin), so the in-memory entry is already a
        # correct tombstone — never surface a raw read error.
        result: dict[str, Any] = {"stdout": "", "stderr": "",
                                  "status": bp.status,
                                  "returncode": bp.returncode}
        if bp.kill_reason is not None:
            result["kill_reason"] = bp.kill_reason
        if wait_ended is not None:
            result["wait_ended"] = wait_ended
        return json.dumps(result, ensure_ascii=False)


def _tombstone_result(ctx: ToolContext, bg_id: str) -> str | None:
    ts = _load_tombstones(_tombstone_path(ctx)).get(bg_id)
    if ts is None:
        return None
    result: dict[str, Any] = {
        "stdout": "", "stderr": "",
        "status": ts.get("status", "killed"),
        "returncode": ts.get("returncode"),
    }
    if ts.get("kill_reason") is not None:
        result["kill_reason"] = ts.get("kill_reason")
    return json.dumps(result, ensure_ascii=False)


async def _wait_for_terminal(
    ctx: ToolContext, bp: BgProc, timeout: float | None,
) -> str:
    raise NotImplementedError  # Task 11


@imp_tool(
    name="bash_output",
    description=(
        "Read incremental output from a background process started with "
        "bash(run_in_background=true). Returns JSON {stdout, stderr, status, "
        "returncode, kill_reason?, wait_ended?}; status is running|exited|"
        "killed. Each call returns only NEW output since the previous call "
        "(per-stream cursors), capped at 50 KB per stream per call; when more "
        "remains, the result carries a notice with per-stream remaining byte "
        "counts and the stream-file paths. Page fully only when the content "
        "matters — for bulk logs (a 5 MB stream is ~100 pages of context), "
        "use bash (grep/tail) or read_file on the stream files instead. An "
        "exited or killed id stays readable until the invocation ends. "
        "wait=true blocks until the process reaches a terminal status, your "
        "timeout elapses, or the 20-minute wait budget is exhausted — "
        "wait_ended says which ('exit'|'timeout'|'budget'); timeout/budget "
        "returns are normal results, and a timeout above the budget is "
        "clipped to it. timeout requires wait=true and must be > 0. Note: "
        "file-redirected output is block-buffered (~4 KB) by most programs, "
        "so very recent output may lag until the child flushes."
    ),
)
async def bash_output(
    ctx: ToolContext, id: str, *, wait: bool = False,
    timeout: float | None = None,
) -> str:
    if timeout is not None and not wait:
        raise ValueError("timeout is only meaningful with wait=true")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be > 0")
    bp = ctx.background_processes.get(id)
    if bp is None:
        # In-memory registry first; else the persisted tombstone file (the
        # registry dies with the runner — a resume re-drives with a fresh
        # ToolContext); else unknown.
        result = _tombstone_result(ctx, id)
        if result is None:
            raise ValueError(f"unknown background process id {id!r}")
        return result
    _reap(bp)
    if not wait:
        return _snapshot_or_tombstone(ctx, bp)
    return await _wait_for_terminal(ctx, bp, timeout)


async def _kill_group_escalate(bp: BgProc) -> None:
    """SIGTERM group -> grace -> SIGKILL group. The final SIGKILL is sent
    UNCONDITIONALLY to the group (kill_process_group suppresses ESRCH on a
    fully-dead group): grace-waiting the LEADER tells us nothing about forked
    children that inherited the write fds. Shielded: a ticker/wait-tick
    cancellation landing mid-grace must not skip the SIGKILL escalation — the
    caller already flipped kill_reason, so no later sweep would retry."""
    with anyio.CancelScope(shield=True):
        await kill_process_group(bp.proc, signal.SIGTERM)
        with anyio.move_on_after(PROCESS_KILL_GRACE_S):
            with contextlib.suppress(Exception):
                await bp.proc.wait()
        await kill_process_group(bp.proc, signal.SIGKILL)
        with anyio.move_on_after(PROCESS_KILL_GRACE_S):
            with contextlib.suppress(Exception):
                await bp.proc.wait()


@imp_tool(
    name="bash_kill",
    description=(
        "Terminate a background process started with "
        "bash(run_in_background=true): SIGTERM to its process group, 4 s "
        "grace, then SIGKILL. Returns a final-status JSON snapshot {stdout, "
        "stderr, status, returncode, kill_reason?} — the snapshot is a real "
        "read and advances the output cursors, so a follow-up bash_output "
        "continues from there without duplicate bytes; remaining output "
        "stays pageable. Idempotent on an already-exited or already-killed "
        "id (returns the current snapshot)."
    ),
)
async def bash_kill(ctx: ToolContext, id: str) -> str:
    bp = ctx.background_processes.get(id)
    if bp is None:
        result = _tombstone_result(ctx, id)
        if result is None:
            raise ValueError(f"unknown background process id {id!r}")
        return result
    _reap(bp)
    if bp.status == "running":
        # Flip-first, BEFORE signaling (same check-and-set discipline as
        # teardown step 1): marking only after the grace await would reopen
        # the sweep-relabel window mid-grace. Decision 6: an agent-requested
        # kill is EXPECTED — no tool_killed event; the real negative-signal
        # returncode flows back as plain data (the provider heuristic is
        # gated on tu.name == "bash" and never sees this result).
        bp.status = "killed"
        bp.kill_reason = "agent"
        await _kill_group_escalate(bp)
        if bp.returncode is None and bp.proc.returncode is not None:
            # status is already terminal, so a vanilla _reap would no-op —
            # record the reaped returncode directly.
            bp.returncode = bp.proc.returncode
    return _snapshot_or_tombstone(ctx, bp)


def _emit_cap_warning(ctx: ToolContext, bp: BgProc) -> None:
    """Decision 6: an output-cap kill is UNEXPECTED (imp unilaterally killing
    the user's process) — emit runner_warning via ctx.event_log, the shared
    EventLog WRITE instance. The ctx field (not a loop-local) is what makes
    emission work identically from provider-loop sweep sites and from inside
    bash_output's wait ticks."""
    if ctx.event_log is None:
        return
    cmd = bp.command[:_CMD_TRUNC]
    ctx.event_log.append(
        agent_id=ctx.agent_id, type="runner_warning",
        payload={
            # message is REQUIRED: the only UI consumer renders
            # payload.get("message", "?") (narrative.py:936).
            "message": (f"background process {bp.id} ({cmd}) killed: "
                        "output cap exceeded"),
            "id": bp.id, "command": cmd, "reason": "output-cap",
        })


async def _sweep_bg(ctx: ToolContext) -> None:
    """Poll exits + enforce the per-stream file cap for EVERY registered
    BgProc — REGARDLESS of status (forking-server rule: an exited leader's
    children inherit the write fds and keep growing the files). Sweep sites
    (Decision 10): every tool dispatch, every wait=True tick, the foreground
    bash ticker, post-API. Reads the cap through the limits module so tests
    (and only tests) can shrink it."""
    for bp in list(ctx.background_processes.values()):
        _reap(bp)
        if bp.kill_reason is not None:
            # Breach handling fires ONCE per process — gate on kill_reason is
            # None. Not status != "killed" (conflicts with regardless-of-
            # status); not kill_reason != "output-cap" (an agent-killed
            # flooder would be re-killed and RELABELED, and a teardown flip
            # could be erased mid-grace). The forking-server case has
            # kill_reason None and stays cap-stat'ed.
            continue
        try:
            over = (os.stat(bp.out_path).st_size > _limits.BG_STREAM_FILE_CAP_BYTES
                    or os.stat(bp.err_path).st_size > _limits.BG_STREAM_FILE_CAP_BYTES)
        except OSError:
            # FileNotFoundError: interrupt-path teardown can delete a stream
            # file between the registry walk and this stat — teardown owns the
            # entry now. Broadened to OSError because the sweep also runs
            # inside the foreground-bash ticker, where ANY raise would crash
            # that call's task group and fail an unrelated foreground command.
            # (bash_output's READ path keeps the narrow FileNotFoundError →
            # tombstone fallthrough; other read errors surface there, per the
            # spec's error-handling table.)
            continue
        if not over:
            continue
        # Flip-first, then signal (check-and-set discipline; see bash_kill).
        # status -> killed RETAINS an already-recorded returncode (exited
        # leader case).
        bp.kill_reason = "output-cap"
        bp.status = "killed"
        _emit_cap_warning(ctx, bp)
        await _kill_group_escalate(bp)
        if bp.returncode is None and bp.proc.returncode is not None:
            bp.returncode = bp.proc.returncode


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
