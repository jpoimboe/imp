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
from pathlib import Path
from uuid import uuid4

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools import limits as _limits
from imp.agents.tools.context import ToolContext, kill_process_tree
from imp.agents.tools.limits import (
    BASH_FOREGROUND_TIMEOUT_MAX_S,
    BASH_FOREGROUND_TIMEOUT_S,
    BASH_OUTPUT_CAP_BYTES,
    tool_checkpoint,
)
from imp.agents.tools.lines import clip_long_lines
from imp.agents.tools.spill import spill_tool_output
from imp.engine.env import engine_env, overlay_engine_env
from imp.engine.sandbox import (
    SANDBOX_SPAWN_FAILED_MESSAGE,
    SANDBOX_SPAWN_MAX_ATTEMPTS,
    build_sandbox_argv,
    prepare_spawn_policy,
    sandbox_spawn_backoff_delay,
    sandbox_spawn_failed,
    strip_sandbox_marker,
)
from imp.engine.shim import imp_shim_dir

_PAUSE_POLL_S = 0.05      # mid-command coordinator.should_pause poll cadence
                          # (a real idle sleep — matches bg.py's _WAIT_POLL_S)
_FG_CAP_POLL_S = 1.0      # foreground own-cap stat cadence (~1 s, mirrors bg's
                          # _sweep_bg trigger; the file-size analogue of the old
                          # combined-byte kill ceiling)
_EVENT_COMMAND_PREVIEW_LEN = 200
_SANDBOX_DENIAL_MARKERS = (
    "read-only file system", "cannot lock ref", "new index file")
_SANDBOX_DENIAL_NOTE = (
    "a write was refused by imp's sandbox, not by the OS at large: shared git "
    "refs (all but `refs/heads/imp/`), the main `.git/index`, and paths outside "
    "this quest's worktree are read-only. Retrying will not help — if the change "
    "belongs in the user's checkout, hand the user the command to run themselves."
)


def _emit_overlay_setup_warning(ctx: ToolContext, command: str) -> None:
    """Best-effort event for an overlay mount-setup failure; never crashes the tool call."""
    if ctx.event_log is None:
        return
    with contextlib.suppress(Exception):
        ctx.event_log.append(
            agent_id=ctx.agent_id, type="runner_warning",
            payload={
                "message": ("the OS sandbox could not start the command; it did "
                            "not run (sandbox-setup failure, not a command exit)"),
                "command": command[:_EVENT_COMMAND_PREVIEW_LEN], "reason": "sandbox-spawn-failed",
            })


def _emit_spawn_retry_warning(ctx: ToolContext, command: str, next_attempt: int) -> None:
    """Best-effort per-retry breadcrumb; never crashes the tool call."""
    if ctx.event_log is None:
        return
    with contextlib.suppress(Exception):
        ctx.event_log.append(
            agent_id=ctx.agent_id, type="runner_warning",
            payload={
                "message": ("sandbox could not start the command; retrying "
                            f"({next_attempt}/{SANDBOX_SPAWN_MAX_ATTEMPTS})"),
                "command": command[:_EVENT_COMMAND_PREVIEW_LEN],
                "reason": "sandbox-spawn-failed",
            })


def _effective_foreground_timeout(timeout: float | None) -> float:
    """Foreground deadline (seconds): the caller's `timeout` or the default,
    clamped to the max. timeout=None/0 -> default; a request above the cap clamps
    down. Only the foreground path consults this — run_in_background returns before
    the deadline is applied, so it is structurally exempt."""
    return min(timeout or BASH_FOREGROUND_TIMEOUT_S, BASH_FOREGROUND_TIMEOUT_MAX_S)


def _sandbox_denied_write(
    stderr: str, *, sandbox_active: bool, returncode: int,
) -> bool:
    """Whether a nonzero exit under an active sandbox looks like a masked-path
    write refusal. Reads the FULL stderr, never the capped display copy."""
    if not sandbox_active or returncode == 0:
        return False
    lowered = stderr.lower()
    return any(marker in lowered for marker in _SANDBOX_DENIAL_MARKERS)


def _clip_cap_notice_clause(clipped_lines: int, over_cap: bool) -> str | None:
    """Leading clause naming what altered the returned output, or None when
    nothing did — there is then no notice to emit and the text is byte-exact.
    The "2000" / "30 KB" literals are MATCH_LINE_CAP_BYTES /
    BASH_OUTPUT_CAP_BYTES in prose (F4 lockstep note in limits.py)."""
    if not clipped_lines:
        return "Output truncated at the 30 KB cap." if over_cap else None
    noun = "line" if clipped_lines == 1 else "lines"
    clip = f"{clipped_lines} long {noun} clipped at 2000 chars"
    if over_cap:
        return f"{clip}, then output truncated at the 30 KB cap."
    return f"{clip}."


def _cap_combined(out: str, err: str) -> tuple[str, str, bool]:
    """Cut stdout/stderr to a COMBINED BASH_OUTPUT_CAP_BYTES, reporting whether
    anything was cut — one function, so the flag cannot drift from the slice.
    Fair share (half each; the smaller stream yields its remainder) keeps a
    chatty stream from starving the other's error line. Byte slicing with
    errors="ignore" drops only the trailing partial sequence, never a U+FFFD."""
    out_bytes = out.encode("utf-8")
    err_bytes = err.encode("utf-8")
    if len(out_bytes) + len(err_bytes) <= BASH_OUTPUT_CAP_BYTES:
        return out, err, False
    half = BASH_OUTPUT_CAP_BYTES // 2
    out_budget = min(len(out_bytes), BASH_OUTPUT_CAP_BYTES - min(len(err_bytes), half))
    err_budget = min(len(err_bytes), BASH_OUTPUT_CAP_BYTES - min(len(out_bytes), half))
    return (out_bytes[:out_budget].decode("utf-8", "ignore"),
            err_bytes[:err_budget].decode("utf-8", "ignore"),
            True)


@imp_tool(
    name="bash",
    description=(
        "Run a shell command. Foreground: returns JSON {stdout, stderr, "
        "returncode}; lines over 2000 chars are clipped with `…[truncated]` and "
        "output over 30 KB is cut short — either way the full output spills to a "
        "file with a `notice` path, and no `notice` means what you got is "
        "byte-exact. `run_in_background=true` returns {id, pid, …} "
        "immediately — read output with `bash_output(id)`, stop with "
        "`bash_kill(id)`; a bad command still spawns then fails, so check "
        "`bash_output` after. A foreground command that exceeds its deadline "
        "(default 120 s; pass `timeout`, positive seconds, max 600, to extend) "
        "is promoted to a background task readable via `bash_output`/killable "
        "via `bash_kill`. No "
        "permission gate. "
        "See `imp-tool-reference` for buffering and path details."
    ),
)
async def bash(
    ctx: ToolContext, command: str, cwd: str | None = None,
    *, run_in_background: bool = False, timeout: float | None = None,
) -> str:
    cwd_path = cwd or str(ctx.project_root)
    # Engine vars come from the LIVE quest when available (top-level steps), so a
    # mid-run rename_quest is reflected immediately; subagents have no live quest
    # (and no rename authority) and fall back to the frozen quest_context snapshot.
    src = ctx.quest or ctx.quest_context
    qdir = (getattr(src, "quest_dir", None)
            or (Path(h) if (h := getattr(src, "quest_home", None)) else None))
    eng = engine_env(
        project_root=ctx.project_root,
        quest_id=src.id if src else "",
        quest_title=src.title if src else "",
        quest_dir=qdir,
        test_gates=ctx.test_gates,
        pythonpath=ctx.pythonpath,
    )
    env = overlay_engine_env(os.environ, eng)
    if qdir is not None:
        env["PATH"] = f"{imp_shim_dir(qdir)}{os.pathsep}{env.get('PATH', '')}"
    policy = ctx.sandbox_policy
    argv = (build_sandbox_argv(command, cwd=cwd_path,
                               policy=prepare_spawn_policy(policy), engine_env=eng)
            if policy is not None else ["sh", "-c", command])
    if run_in_background:
        if not ctx.background_enabled:
            # Future-provider defense: a provider exposing registry bash without
            # the bg wiring (injection + sweeps + teardown) gets a loud refusal
            # with the foreground alternative rather than an unmanageable orphan.
            raise ValueError(
                "run_in_background is not supported under this provider's "
                "tool loop; run the command in the foreground instead")
        from imp.agents.tools.bg import spawn_background
        return await spawn_background(
            ctx, command=command, argv=argv, cwd=cwd_path, env=env)
    # §4: a pending pause must not spawn the child (mirrors the bg-process contract).
    coordinator = getattr(ctx, "coordinator", None)
    await tool_checkpoint(coordinator, None)

    # Foreground now spawns to scratch FILES via the shared core (like bg.py), not
    # pipes: this is what makes a deadline-exceeding command promotable (Part 3)
    # and gives foreground stdin=DEVNULL for free. fg-<uuid> naming keeps a fast
    # foreground call from consuming a bgN id; Part 3 renames these to bgN on
    # promotion. The M1 invariant holds: the core spawns start_new_session, so
    # pgid == pid and terminate_running_processes / kill_process_group can killpg.
    from imp.agents.tools.bg import BgProc, _bg_dir, _seed_counter, _spawn_to_files
    d = _bg_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)

    async def _run_once(attempt: int) -> str | None:
        stem = f"fg-{uuid4().hex}"
        out_path = d / f"{stem}.out"
        err_path = d / f"{stem}.err"
        proc = await _spawn_to_files(
            argv, out_path=out_path, err_path=err_path, cwd=cwd_path, env=env)
        ctx.running_processes.append(proc)

        # Decision 10 ticker: sweeps the background registry while THIS foreground call
        # blocks the loop — the only coverage for a long QUIET command. Own
        # CancelScope, cancelled right after proc.wait() (an uncancelled ticker would
        # hold the task group open to the fail_after; cancelling the whole group would
        # race the other pollers). Skipped when the registry is empty.
        has_bg = bool(ctx.background_processes)
        ticker_scope = anyio.CancelScope()

        async def _bg_ticker() -> None:
            from imp.agents.tools.bg import _sweep_bg
            with ticker_scope:
                while True:
                    await anyio.sleep(1.0)
                    await _sweep_bg(ctx)

        # Foreground own-cap check: the file-model analogue of the old combined-byte
        # ceiling. On the ~1 s cadence, stat BOTH scratch files; if their COMBINED
        # size crosses BG_STREAM_FILE_CAP_BYTES (read through _limits so tests can
        # shrink it, matching _sweep_bg), SIGKILL the group (single-shot, wait-free,
        # so proc.wait() returns) and flag `capped`. A cap-kill is cleanup, not a
        # tool_killed signal — the result carries returncode 0. Own CancelScope,
        # cancelled after proc.wait().
        capped = False
        capcheck_scope = anyio.CancelScope()

        async def _own_cap_poller() -> None:
            nonlocal capped
            with capcheck_scope:
                while True:
                    await anyio.sleep(_FG_CAP_POLL_S)
                    try:
                        total = (os.stat(out_path).st_size
                                 + os.stat(err_path).st_size)
                    except OSError:
                        continue
                    if total > _limits.BG_STREAM_FILE_CAP_BYTES:
                        capped = True
                        await kill_process_tree(proc, signal.SIGKILL)
                        return

        # §4: a pause landing MID-COMMAND -> SIGKILL the group so proc.wait() returns,
        # and flag the call to return the terminated-by-pause tombstone. Own
        # CancelScope, cancelled synchronously the instant proc.wait() returns so a
        # pause flipping right after a natural completion cannot tombstone it.
        paused = False
        pause_scope = anyio.CancelScope()

        async def _pause_poller() -> None:
            nonlocal paused
            assert coordinator is not None      # poller only started under this guard
            with pause_scope:
                while True:
                    await anyio.sleep(_PAUSE_POLL_S)
                    if coordinator.should_pause and proc.returncode is None:
                        paused = True
                        await kill_process_tree(proc, signal.SIGKILL)
                        return

        try:
            try:
                with anyio.fail_after(_effective_foreground_timeout(timeout)):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_own_cap_poller)
                        if has_bg:
                            tg.start_soon(_bg_ticker)
                        if coordinator is not None:
                            tg.start_soon(_pause_poller)
                        await proc.wait()
                        capcheck_scope.cancel()
                        if has_bg:
                            ticker_scope.cancel()
                        if coordinator is not None:
                            pause_scope.cancel()
            except TimeoutError:
                # Deadline exceeded: PROMOTE, don't kill. Seed the counter, allocate a
                # canonical bgN, and same-dir rename the fg-<uuid> scratch files to
                # bgN.* — the child's open write fds stay valid (unchanged inode), so it
                # keeps writing to the renamed paths. Canonical bgN naming is REQUIRED:
                # resume counter-seeding scans stream filenames (_FILE_RE) to never
                # re-alias a transcript-referenced id, and a promoted id is exactly such
                # an id. Wrap the still-running proc in a BgProc and register it; the
                # `finally` removes it from running_processes, so it is now managed
                # solely through the background lifecycle.
                _seed_counter(ctx)
                bg_id = ctx.next_bg_id()
                new_out = d / f"{bg_id}.out"
                new_err = d / f"{bg_id}.err"
                os.rename(out_path, new_out)
                os.rename(err_path, new_err)
                ctx.background_processes[bg_id] = BgProc(
                    id=bg_id, proc=proc, out_path=new_out, err_path=new_err,
                    command=command)
                root = ctx.project_root.resolve()
                deadline_s = _effective_foreground_timeout(timeout)
                note = (
                    f"foreground command exceeded its {deadline_s:g} s deadline and was "
                    f'moved to the background; read it with bash_output("{bg_id}") and '
                    f'stop it with bash_kill("{bg_id}")')
                return json.dumps({
                    "id": bg_id,
                    "pid": proc.pid,
                    "command": command,
                    "status": "running",
                    "note": note,
                    "out_path": os.path.relpath(new_out.resolve(), root),
                    "err_path": os.path.relpath(new_err.resolve(), root),
                }, ensure_ascii=False)
        finally:
            with contextlib.suppress(ValueError):
                ctx.running_processes.remove(proc)

        # Whole-file read (NOT bg.py's cursor-paged _read_page — that reader is for
        # incremental background tailing). Bounded by the own-cap kill above. Then
        # unlink the scratch files: a non-promoted foreground proc leaves nothing.
        out_bytes = out_path.read_bytes()
        err_bytes = err_path.read_bytes()
        for p in (out_path, err_path):
            with contextlib.suppress(OSError):
                p.unlink()
        stdout = out_bytes.decode("utf-8", "replace")
        stderr = err_bytes.decode("utf-8", "replace")
        # Detect BEFORE stripping (marker absence is the signal), guarded on whether
        # the sandbox was active for THIS spawn, then strip so no agent sees the marker.
        spawn_failed = sandbox_spawn_failed(
            stderr, sandbox_active=policy is not None and policy.active)
        stderr = strip_sandbox_marker(stderr)
        if spawn_failed and attempt < SANDBOX_SPAWN_MAX_ATTEMPTS:
            # The sandbox never entered the command, so re-spawning is provably safe
            # (the command never ran). Signal the outer loop to retry after backoff.
            return None
        # Clip-then-cap, decided ONCE for every branch below: clipping first is what
        # keeps a pathological line from eating the whole 30 KB budget, and routing
        # every return through _finish is what makes the advertised invariant
        # structural — absence of a `notice` means the returned text is byte-exact.
        clipped_out, n_out = clip_long_lines(stdout)
        clipped_err, n_err = clip_long_lines(stderr)
        disp_out, disp_err, over_cap = _cap_combined(clipped_out, clipped_err)
        clause = _clip_cap_notice_clause(n_out + n_err, over_cap)
        notice: str | None = None
        if clause is not None:
            # The spill body is the RAW decoded output — unclipped and uncapped — so
            # the exact bytes stay recoverable with an ordinary read_file.
            full = (f"=== stdout ===\n{stdout}\n=== stderr ===\n{stderr}"
                    if stderr else stdout)
            notice = f"{clause} {spill_tool_output(ctx, uuid4().hex, full, preview_bytes=0)}"

        def _finish(**fields: object) -> str:
            result: dict[str, object] = {
                "stdout": disp_out,
                "stderr": disp_err,
                **fields,
            }
            if notice is not None:
                result["notice"] = notice
            return json.dumps(result, ensure_ascii=False)

        if spawn_failed:
            # Retries exhausted: report distinctly. Such a failure exits immediately,
            # so it never reaches promotion.
            _emit_overlay_setup_warning(ctx, command)
            return _finish(status="sandbox_setup_failed",
                           error=SANDBOX_SPAWN_FAILED_MESSAGE,
                           returncode=proc.returncode)
        if paused:
            # A pause landed mid-command: the poller SIGKILL'd the group. Return the
            # terminated-by-pause tombstone (the same shape
            # terminate_background_processes writes). returncode stays the real
            # negative signal (an honest kill — unlike the cap path).
            return _finish(
                status="killed", kill_reason="interrupted",
                returncode=(proc.returncode if proc.returncode is not None
                            else -signal.SIGKILL))
        # returncode 0 on a cap-kill — a cap-truncation is a NORMAL result; the
        # SIGKILL we issued was cleanup and must NOT read as tool_killed (the
        # provider keys that on returncode < 0).
        returncode = 0 if capped else (
            proc.returncode if proc.returncode is not None else -signal.SIGKILL)
        if _sandbox_denied_write(
                stderr, sandbox_active=policy is not None and policy.active,
                returncode=returncode):
            return _finish(returncode=returncode,
                           sandbox_note=_SANDBOX_DENIAL_NOTE)
        return _finish(returncode=returncode)

    for attempt in range(1, SANDBOX_SPAWN_MAX_ATTEMPTS + 1):
        result = await _run_once(attempt)
        if result is not None:
            return result
        _emit_spawn_retry_warning(ctx, command, attempt + 1)
        await anyio.sleep(sandbox_spawn_backoff_delay(attempt))
    raise AssertionError("foreground retry loop must return on the final attempt")
