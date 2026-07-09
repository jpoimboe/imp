"""bash tool (spec §6.6 + §6.15).

Runs a shell command via anyio.run_process in its own session (POSIX), so the
process group can be SIGTERM/SIGKILL'd from the runner's interrupt path. The
process is registered on ctx.running_processes for the duration; the runner's
terminate_running_processes() walks the registry on interrupt.

No FS permission gate (shell access is total at OS-user privilege; see spec
§6.7 "bash bypasses FS permissions").
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from subprocess import PIPE
from uuid import uuid4

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext, kill_process_tree
from imp.agents.tools.limits import (
    BG_STREAM_FILE_CAP_BYTES,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    ToolAborted,
    tool_checkpoint,
)
from imp.agents.tools.spill import spill_tool_output
from imp.engine.env import engine_env, overlay_engine_env
from imp.engine.sandbox import build_sandbox_argv


@dataclass
class _Counter:
    """Shared combined-output state for the two drain tasks. `total` is the
    running byte count across BOTH sinks; `capped` flips once it crosses the
    kill ceiling (BG_STREAM_FILE_CAP_BYTES) and the process group is killed."""

    total: int = 0
    capped: bool = False


_PAUSE_POLL_S = 0.05      # mid-command coordinator.should_pause poll cadence
                          # (a real idle sleep — matches bg.py's _WAIT_POLL_S)


@imp_tool(
    name="bash",
    description=(
        "Run a shell command. Foreground (default): returns JSON {stdout, "
        "stderr, returncode}; each displayed stream is the 50 KB head — when "
        "combined output exceeds 50 KB the full output is spilled to a file and "
        "a `notice` field carries a read_file-able path to recover the tail "
        "(runaway output is killed at 5 MB); interruptible (SIGTERM to the "
        "process group, 4 s grace, SIGKILL). "
        "With run_in_background=true: returns immediately with JSON {id, pid, "
        "command, status, out_path, err_path} while the process keeps "
        "running; read incremental output with bash_output(id), stop it with "
        "bash_kill(id). The spawn return does not validate the command — a "
        "typo'd command still spawns and fails milliseconds later, so check "
        "bash_output shortly after spawning. Background output is "
        "file-redirected, so most programs block-buffer (~4 KB): early lines "
        "(e.g. a server's 'listening' banner) may lag until a flush. "
        "out_path/err_path are relative to the project root (bash's default "
        "cwd) and readable via bash or read_file; grep/glob exclude them "
        "(.imp/). No permission gate (shell access is total at OS-user level)."
    ),
)
async def bash(
    ctx: ToolContext, command: str, cwd: str | None = None,
    *, run_in_background: bool = False,
) -> str:
    cwd_path = cwd or str(ctx.project_root)
    # Engine vars come from the LIVE Change when available (top-level steps), so a
    # mid-run rename_change is reflected immediately; subagents have no live Change
    # (and no rename authority) and fall back to the frozen change_context snapshot.
    # change_context is a render-time snapshot that rename_change never updates, so
    # reading it here would leak a stale (empty name-on-intent) title.
    src = ctx.change or ctx.change_context
    eng = engine_env(
        project_root=ctx.project_root,
        change_id=src.id if src else "",
        change_title=src.title if src else "",
        change_dir=(getattr(src, "change_dir", None)
                    or (Path(h) if (h := getattr(src, "change_home", None)) else None)),
        test_gates=ctx.test_gates,
    )
    env = overlay_engine_env(os.environ, eng)
    # POSIX: start_new_session=True makes the child a new session leader so
    # SIGTERM to the PG affects the whole tree. Windows: this kwarg is ignored
    # and signal handling differs — out of V1 scope (we ship POSIX-first).
    kwargs: dict[str, object] = {}
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True

    # anyio.open_process accepts stdlib subprocess constants for stdin/stdout/
    # stderr; PIPE is the documented way to capture both streams.
    policy = ctx.sandbox_policy
    argv = (build_sandbox_argv(command, cwd=cwd_path, policy=policy, engine_env=eng)
            if policy is not None else ["sh", "-c", command])
    if run_in_background:
        if not ctx.background_enabled:
            # Future-provider defense: all four current providers wire the
            # full bg surface and set the flag, so this is unreachable in
            # production today. A NEW provider that exposes registry bash
            # without the wiring (injection + sweeps + teardown) gets a loud
            # refusal with the foreground alternative rather than spawning an
            # unmanageable, unsweepable, leak-past-exit process.
            raise ValueError(
                "run_in_background is not supported under this provider's "
                "tool loop; run the command in the foreground instead")
        # Same engine_env overlay and sandbox argv as foreground; only the
        # stdio plumbing differs. Lazy import: bg is a sibling tool module
        # registered by the same _register_builtins pass.
        from imp.agents.tools.bg import spawn_background
        return await spawn_background(
            ctx, command=command, argv=argv, cwd=cwd_path, env=env)
    # §4: a pending pause must not spawn the child (mirrors the bg-process contract).
    coordinator = getattr(ctx, "coordinator", None)
    await tool_checkpoint(coordinator, None)
    proc = await anyio.open_process(
        argv,
        stdin=None,
        stdout=PIPE,
        stderr=PIPE,
        cwd=cwd_path,
        env=env,
        **kwargs,  # type: ignore[arg-type]
    )
    # M1 invariant: every proc registered here is a setsid session leader
    # (start_new_session above), so pgid == pid and terminate_running_processes /
    # kill_process_group can os.killpg(pid, …) without os.getpgid. If a future
    # caller ever registers a NON-setsid proc, revisit kill_process_group.
    ctx.running_processes.append(proc)
    kill_cap = BG_STREAM_FILE_CAP_BYTES
    # Combined running counter across BOTH sinks: counting separately would let
    # the total reach 2x the ceiling. When the combined total crosses the 5 MB
    # kill ceiling and the process is still producing, kill the GROUP (cleanup)
    # and stop draining. A foreground process is short-lived, so the ~5 MB peak
    # buffer is the same bound background processes already accept.
    # N5: peak buffered bytes is kill_cap + up to one in-flight chunk PER stream
    # (both drains can append before either observes `capped`), i.e.
    # <= ~5 MB + 2*READ chunk. The 50 KB display truncation happens later, on the
    # assemble path, independent of whether this ceiling was hit.
    # Single mutable record shared by both drains. Typed fields (not a bag of
    # `object`) so the arithmetic below stays mypy-clean.
    counter = _Counter()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _drain(stream: object, sink: list[bytes]) -> None:
        if stream is None:
            return
        # Post-cap the loop keeps consuming (discarding) to EOF: asyncio only
        # resolves proc.wait() once every pipe transport disconnects, and an
        # abandoned stream gets flow-control-paused on the killed child's pipe
        # residue — its fd leaves the selector, EOF is never seen, and wait()
        # deadlocks to the tool wall-clock. Residue is bounded (kernel pipe +
        # reader buffer), so this costs a few reads, not another flood.
        async for chunk in stream:  # type: ignore[attr-defined]
            if counter.capped:
                continue
            sink.append(chunk)
            counter.total += len(chunk)
            if counter.total >= kill_cap:
                counter.capped = True
                await kill_process_tree(proc, signal.SIGKILL)

    # Decision 10 ticker (pinned lifecycle): sweeps the background registry
    # while THIS foreground call runs — the only coverage for a long QUIET
    # command (no chunks => the drain bodies never run; chatty-then-silent
    # EOFs both drains and sits in proc.wait()). It runs in its OWN
    # CancelScope, cancelled right after proc.wait() returns: an uncancelled
    # ticker would hold the task group open to the B.1 fail_after (every
    # quick bash "times out"), while cancelling the whole group would also
    # cancel the drains mid-flush (silent tail truncation). Not a long-lived-
    # task violation: scoped inside one call's own task group, exactly like
    # the drains.
    # Skip ticker when empty: foreground bash owns the loop, so an empty registry stays empty.
    has_bg = bool(ctx.background_processes)
    ticker_scope = anyio.CancelScope()

    async def _bg_ticker() -> None:
        from imp.agents.tools.bg import _sweep_bg
        with ticker_scope:
            while True:
                await anyio.sleep(1.0)
                await _sweep_bg(ctx)

    # §4: watch for a pause landing MID-COMMAND. On one, SIGKILL the child's
    # group (the same wait-free single-shot kill the cap/timeout paths use) so
    # proc.wait() returns, and flag the call to return the terminated-by-pause
    # tombstone instead of a normal result. Runs in its own CancelScope,
    # cancelled right after proc.wait() returns (same discipline as _bg_ticker).
    # The poller only sets `paused` while proc.returncode is None (still
    # running), so a completed command can never be tombstoned by a stray tick.
    paused = False
    pause_scope = anyio.CancelScope()

    async def _pause_poller() -> None:
        nonlocal paused
        assert coordinator is not None      # poller only started under this guard
        with pause_scope:
            while True:
                await anyio.sleep(_PAUSE_POLL_S)
                # proc.returncode is None => still running: a stray final tick
                # after a natural completion (returncode already set) cannot
                # tombstone a just-finished command.
                if coordinator.should_pause and proc.returncode is None:
                    paused = True
                    await kill_process_tree(proc, signal.SIGKILL)
                    return

    try:
        try:
            with anyio.fail_after(TOOL_WALLCLOCK_TIMEOUT_S):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_drain, proc.stdout, stdout_chunks)
                    tg.start_soon(_drain, proc.stderr, stderr_chunks)
                    if has_bg:
                        tg.start_soon(_bg_ticker)
                    if coordinator is not None:
                        tg.start_soon(_pause_poller)
                    await proc.wait()
                    if has_bg:
                        ticker_scope.cancel()
                    if coordinator is not None:
                        # Cancel synchronously the instant proc.wait() returns:
                        # a natural completion must stop the poller before its
                        # next tick, so a pause flipping right after cannot set
                        # `paused` on an already-finished command.
                        pause_scope.cancel()
        except TimeoutError:
            # B.1: the call outran the wall clock. Kill the group, reap, and
            # surface a timeout the agent can react to (NOT a normal result).
            await kill_process_tree(proc, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise ToolAborted("timed-out") from None
    finally:
        with contextlib.suppress(ValueError):
            ctx.running_processes.remove(proc)

    out_bytes = b"".join(stdout_chunks)
    err_bytes = b"".join(stderr_chunks)
    stdout = out_bytes.decode("utf-8", "replace")
    stderr = err_bytes.decode("utf-8", "replace")
    if paused:
        # A pause landed mid-command: the poller SIGKILL'd the group. Return the
        # terminated-by-pause tombstone (status/kill_reason) — the same shape
        # terminate_background_processes writes — so a resumed agent understands
        # why its command died. returncode stays the real negative signal (an
        # honest kill: unlike the cap path, this IS why the command died, so the
        # provider's returncode<0 -> tool_killed heuristic firing is correct).
        result: dict[str, object] = {
            "stdout": stdout,
            "stderr": stderr,
            "status": "killed",
            "kill_reason": "interrupted",
            "returncode": (proc.returncode if proc.returncode is not None
                           else -signal.SIGKILL),
        }
    else:
        # returncode 0 on a cap-kill — a cap-truncation is a NORMAL result; the
        # SIGKILL we issued was cleanup and must NOT read as `tool_killed` (the
        # provider keys that on returncode < 0; see anthropic.py:704).
        returncode = 0 if counter.capped else (
            proc.returncode if proc.returncode is not None else -signal.SIGKILL)
        result = {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
        }
    if len(out_bytes) + len(err_bytes) > TOOL_OUTPUT_CAP_BYTES:
        # Over the 50 KB display cap: spill the FULL decoded output so the
        # truncated tail is recoverable with an ordinary read_file, then bound
        # each displayed stream to the 50 KB head. When stderr is non-empty the
        # spill file carries both tails under labeled sections; otherwise it is
        # just the full stdout. The notice supersedes the old inline marker. This
        # also covers the pause tombstone: a pause landing mid-flood must not
        # return up to the 5 MB kill ceiling inline.
        full = (f"=== stdout ===\n{stdout}\n=== stderr ===\n{stderr}"
                if stderr else stdout)
        result["notice"] = spill_tool_output(
            ctx, uuid4().hex, full, preview_bytes=0)
        result["stdout"] = stdout[:TOOL_OUTPUT_CAP_BYTES]
        result["stderr"] = stderr[:TOOL_OUTPUT_CAP_BYTES]

    return json.dumps(result, ensure_ascii=False)
