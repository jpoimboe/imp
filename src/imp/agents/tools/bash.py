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
from subprocess import PIPE

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext, kill_process_group
from imp.agents.tools.limits import (
    OUTPUT_TRUNCATION_MARKER,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    ToolAborted,
)
from imp.engine.env import engine_env, overlay_engine_env
from imp.engine.sandbox import build_sandbox_argv


@dataclass
class _Counter:
    """Shared combined-output state for the two drain tasks. `total` is the
    running byte count across BOTH sinks; `capped` flips once it crosses the cap;
    `which` records the stream that crossed it (so the marker lands on it)."""

    total: int = 0
    capped: bool = False
    which: str = "stdout"


@imp_tool(
    name="bash",
    description=(
        "Run a shell command. Foreground (default): returns JSON {stdout, "
        "stderr, returncode}; combined output capped at 50 KB; interruptible "
        "(SIGTERM to the process group, 4 s grace, SIGKILL). "
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
    env = overlay_engine_env(
        os.environ,
        engine_env(project_root=ctx.project_root,
                   change_id=ctx.change_context.id if ctx.change_context else "",
                   change_slug=ctx.change_context.slug if ctx.change_context else "",
                   change_title=ctx.change_context.title if ctx.change_context else "",
                   lint_command=ctx.lint_command,
                   test_command=ctx.test_command),
    )
    # POSIX: start_new_session=True makes the child a new session leader so
    # SIGTERM to the PG affects the whole tree. Windows: this kwarg is ignored
    # and signal handling differs — out of V1 scope (we ship POSIX-first).
    kwargs: dict[str, object] = {}
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True

    # anyio.open_process accepts stdlib subprocess constants for stdin/stdout/
    # stderr; PIPE is the documented way to capture both streams.
    policy = ctx.sandbox_policy
    argv = (build_sandbox_argv(command, cwd=cwd_path, policy=policy)
            if policy is not None else ["sh", "-c", command])
    if run_in_background:
        if not ctx.background_enabled:
            # Reachable from providers that expose registry bash without the
            # bg wiring (claude_code/codex MCP wrapping): refuse loudly with
            # the foreground alternative rather than spawn an unmanageable,
            # unsweepable, leak-past-exit process.
            raise ValueError(
                "run_in_background is not supported under this provider's "
                "tool loop; run the command in the foreground instead")
        # Same engine_env overlay and sandbox argv as foreground; only the
        # stdio plumbing differs. Lazy import: bg is a sibling tool module
        # registered by the same _register_builtins pass.
        from imp.agents.tools.bg import spawn_background
        return await spawn_background(
            ctx, command=command, argv=argv, cwd=cwd_path, env=env)
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
    cap = TOOL_OUTPUT_CAP_BYTES
    # Combined running counter across BOTH sinks: counting separately would let
    # the total reach 2x the cap. When the combined total crosses the cap and the
    # process is still producing, kill the GROUP (cleanup) and stop draining.
    # N5: peak buffered bytes is cap + up to one in-flight chunk PER stream (both
    # drains can append before either observes `capped`), i.e. <= ~cap + 2*READ
    # chunk (~50 KB + ~128 KB). Bounded and expected — that is the "slop" the
    # final stdout[:cap] slice trims off before returning.
    # Single mutable record shared by both drains. Typed fields (not a bag of
    # `object`) so the `int(...)` arithmetic below stays mypy-clean.
    counter = _Counter()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _drain(stream: object, sink: list[bytes], name: str) -> None:
        if stream is None:
            return
        async for chunk in stream:  # type: ignore[attr-defined]
            if counter.capped:
                return
            sink.append(chunk)
            counter.total += len(chunk)
            if counter.total >= cap:
                counter.capped = True
                counter.which = name           # S2: which stream crossed the cap
                await kill_process_group(proc, signal.SIGKILL)
                return

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
    ticker_scope = anyio.CancelScope()

    async def _bg_ticker() -> None:
        from imp.agents.tools.bg import _sweep_bg
        with ticker_scope:
            while True:
                await anyio.sleep(1.0)
                await _sweep_bg(ctx)

    try:
        try:
            with anyio.fail_after(TOOL_WALLCLOCK_TIMEOUT_S):
                async with anyio.create_task_group() as tg:
                    # NOTE: keep the "stdout"/"stderr" name args from Task 6 —
                    # they drive the S2 capped-stream marker (counter.which).
                    tg.start_soon(_drain, proc.stdout, stdout_chunks, "stdout")
                    tg.start_soon(_drain, proc.stderr, stderr_chunks, "stderr")
                    tg.start_soon(_bg_ticker)
                    await proc.wait()
                    ticker_scope.cancel()
        except TimeoutError:
            # B.1: the call outran the wall clock. Kill the group, reap, and
            # surface a timeout the agent can react to (NOT a normal result).
            await kill_process_group(proc, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise ToolAborted("timed-out") from None
    finally:
        with contextlib.suppress(ValueError):
            ctx.running_processes.remove(proc)

    stdout = b"".join(stdout_chunks).decode("utf-8", "replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    if counter.capped:
        # Head-only: bound BOTH decoded streams to the cap and append the marker to
        # the one that actually crossed it (S2: a stderr-dominant flood must not
        # leave a "truncated" marker on an empty stdout). Report returncode 0 — a
        # cap-truncation is a NORMAL result; the SIGKILL we issued was cleanup and
        # must NOT read as `tool_killed` (the provider keys that on returncode < 0;
        # see anthropic.py:704).
        stdout = stdout[:cap]
        stderr = stderr[:cap]
        if counter.which == "stderr":
            stderr += OUTPUT_TRUNCATION_MARKER
        else:
            stdout += OUTPUT_TRUNCATION_MARKER
        returncode = 0
    else:
        returncode = proc.returncode if proc.returncode is not None else -signal.SIGKILL

    return json.dumps({
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
    }, ensure_ascii=False)
