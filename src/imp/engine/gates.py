# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Step-body helpers: run a script step, evaluate a rules block."""
from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.env import overlay_engine_env
from imp.engine.expr import EvalEnv, evaluate
from imp.engine.sandbox import SandboxPolicy, build_sandbox_argv, prepare_spawn_policy
from imp.engine.types import Action, Continue, Rule, Step

GATE_KILL_GRACE_S = 2.0  # SIGTERM-to-SIGKILL grace window for a gate subprocess group
_KILL_POLL_INTERVAL_S = 0.05  # group-liveness probe cadence inside the grace window
_POLL_INTERVAL_S = 0.5  # max interrupt-to-kill latency: one poll tick
_READ_CHUNK_BYTES = 64 * 1024  # per os.read() chunk off each stream
GATE_OUTPUT_CAP_BYTES = 128 * 1024 * 1024  # total stdout+stderr retained before teardown
GATE_HARD_CEILING_S = 1800  # wall-clock deadline used only when timeout_seconds is None


class ScriptInterrupted(Exception):
    """Raised by run_command when its abort predicate fires mid-run. The process
    group has already been torn down gracefully; the caller treats this as a
    pre-navigation pause, not a gate verdict."""


class ScriptOutputExceeded(subprocess.SubprocessError):
    """Raised by run_command when a gate's total stdout+stderr exceeds
    GATE_OUTPUT_CAP_BYTES. The process group has already been torn down
    gracefully; the caller lets it propagate to the executor's last-resort guard,
    exactly as a gate TimeoutExpired does today (no dedicated pause verb)."""


def _graceful_kill(proc: subprocess.Popen[bytes]) -> None:
    """Tear down a gate subprocess GROUP gracefully, then close its read FDs.

    SIGTERM the group (with start_new_session=True, proc.pid is the group
    leader, so this reaches the whole bwrap -> sh -> make -> pytest -> xdist
    tree), then poll the GROUP's liveness — not proc's exit — for
    GATE_KILL_GRACE_S: under a sandbox the outer bwrap exits on SIGTERM at
    ~0.00s while its children are still shutting down, so keying escalation on
    proc.wait() would skip the SIGKILL entirely and leak them. Each tick reaps
    proc non-blockingly and then probes with killpg(pid, 0) — reaping FIRST is
    what makes a group whose leader has just exited read as empty, which is the
    short-circuit that leaves the window to the children. A group still alive at
    the deadline is SIGKILLed unconditionally, then reaped; that final wait
    cannot deadlock on a full pipe, since a SIGKILLed process is already dead.
    Closing proc.stdout/proc.stderr releases the parent-side read FDs that a
    timed-out or aborted selector drain leaves open."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + GATE_KILL_GRACE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
        time.sleep(_KILL_POLL_INTERVAL_S)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


@dataclass(frozen=True, slots=True)
class RuleMatch:
    action: Action
    rule: Rule | None                       # None for the continue-on-success default
    var_mutations: dict[str, object]        # applied set: results (for the event payload)


def evaluate_rules(
    rules: tuple[Rule, ...], *, vars: dict[str, Any], output: dict[str, Any] | None,
) -> RuleMatch:
    """First matching rule wins; apply its set: mutations and return them.

    No rules means continue-on-success (the agent default). The loader guarantees a
    trailing else, so a non-empty block always matches.
    """
    if not rules:
        return RuleMatch(action=Continue(), rule=None, var_mutations={})
    env = EvalEnv(vars=vars, output=output)
    for rule in rules:
        if rule.condition is None or bool(evaluate(rule.condition, env)):
            applied: dict[str, object] = {}
            for var_name, expr in rule.mutations:
                applied[var_name] = evaluate(expr, env)
            return RuleMatch(action=rule.action, rule=rule, var_mutations=applied)
    # unreachable given the trailing-else invariant
    raise AssertionError("no rule matched and no else (validator should have caught this)")


def run_command(
    *, command: str, cwd: str, timeout_seconds: int | None,
    env_extra: dict[str, str] | None = None,
    policy: SandboxPolicy | None = None,
    abort: Callable[[], bool] | None = None, shim_dir: str = "",
) -> dict[str, Any]:
    """Run one shell command; return {exit, stdout, stderr}.

    The command string is an explicit shell expression read live from the
    quest's own worktree (principal-based trust: the quest author and workflow
    author are the same V0 principal). When `policy` is active it is wrapped with
    bwrap (build_sandbox_argv appends `sh -c <sentinel script> <command>`, the
    command its own argv element); otherwise the bare `sh -c` runs.

    Reaping (spec B.9/H-A): because the sandbox drops --die-with-parent, a
    timeout must kill the process GROUP, not just the bwrap pid — else the inner
    `sh` is orphaned. So this uses Popen(start_new_session=True) + an owned
    selectors drain loop and a graceful group kill (SIGTERM -> grace -> SIGKILL)
    on the timeout, abort, and output-cap paths. `cwd` must lie inside the
    writable set (worktree/`/tmp`); a command pointed at the RO floor fails.

    `env_extra` (an engine_env result) is overlaid authoritatively so engine vars
    are visible while a stale inherited engine var cannot leak (engine_env's
    "absent => no gate" contract).
    """
    env = overlay_engine_env(os.environ, env_extra or {})
    if shim_dir:
        env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    # The policy's setenv was frozen at run start (possibly with an empty
    # name-on-intent slug), so the live engine_env (env_extra) is the authoritative
    # source that refreshes IMP_* inside the bwrap --clearenv jail.
    argv = (build_sandbox_argv(command, cwd=cwd, policy=prepare_spawn_policy(policy),
                               engine_env=env_extra or {})
            if policy is not None else ["sh", "-c", command])
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True,
    )
    # Owned selector drain: os.read chunks off stdout/stderr as they arrive (so a
    # chatty child never deadlocks on a full pipe) into per-stream bytearrays with
    # a running total. communicate() is avoided precisely because it retains
    # everything internally with no exposed byte count — which is what let a
    # wedged gate balloon RSS unbounded. The abort predicate and the wall-clock
    # deadline are evaluated on a _POLL_INTERVAL_S cadence (one check per tick,
    # NOT per read) so interrupt/timeout latency matches the old
    # communicate(timeout=_POLL_INTERVAL_S) loop exactly; within a tick every
    # ready stream is drained fully (non-blocking reads until EWOULDBLOCK/EOF).
    # The output cap is checked after every chunk, so worst-case retained memory
    # is cap + one chunk. All three terminal paths tear the group down via
    # _graceful_kill. A stream returning EOF is unregistered; the loop ends once
    # the child has exited and both streams are drained.
    assert proc.stdout is not None and proc.stderr is not None
    start = time.monotonic()
    timeout = (timeout_seconds if timeout_seconds is not None
               else GATE_HARD_CEILING_S)
    out_buf = bytearray()
    err_buf = bytearray()
    total = 0
    out_fd = proc.stdout.fileno()
    err_fd = proc.stderr.fileno()
    os.set_blocking(out_fd, False)
    os.set_blocking(err_fd, False)
    bufs: dict[int, bytearray] = {out_fd: out_buf, err_fd: err_buf}
    selector = selectors.DefaultSelector()
    try:
        selector.register(out_fd, selectors.EVENT_READ)
        selector.register(err_fd, selectors.EVENT_READ)
        # First abort/deadline check falls one tick in, matching the old loop
        # (which checked only after its first communicate(timeout=_POLL_INTERVAL_S)
        # tick).
        next_check = start + _POLL_INTERVAL_S
        while proc.poll() is None or selector.get_map():
            now = time.monotonic()
            if now >= next_check:
                if abort is not None and abort():
                    _graceful_kill(proc)
                    raise ScriptInterrupted from None
                if now - start >= timeout:
                    _graceful_kill(proc)
                    raise subprocess.TimeoutExpired(
                        proc.args, timeout,
                        output=out_buf.decode("utf-8", "replace").rstrip(),
                        stderr=err_buf.decode("utf-8", "replace"),
                    ) from None
                next_check = now + _POLL_INTERVAL_S
            if selector.get_map():
                for key, _mask in selector.select(_POLL_INTERVAL_S):
                    while True:
                        try:
                            chunk = os.read(key.fd, _READ_CHUNK_BYTES)
                        except BlockingIOError:
                            break
                        if not chunk:
                            selector.unregister(key.fd)
                            break
                        bufs[key.fd].extend(chunk)
                        total += len(chunk)
                        if total > GATE_OUTPUT_CAP_BYTES:
                            _graceful_kill(proc)
                            raise ScriptOutputExceeded(
                                f"gate output exceeded {GATE_OUTPUT_CAP_BYTES} bytes"
                            ) from None
            else:
                time.sleep(_POLL_INTERVAL_S)
        proc.wait()
    finally:
        selector.close()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    return {
        "exit": proc.returncode,
        "stdout": out_buf.decode("utf-8", "replace").rstrip(),
        "stderr": err_buf.decode("utf-8", "replace"),
    }


def run_script(
    step: Step, *, cwd: Path, env_extra: dict[str, str] | None = None,
    policy: SandboxPolicy | None = None, command: str | None = None,
    abort: Callable[[], bool] | None = None, shim_dir: str = "",
) -> dict[str, Any]:
    """Unpack a script step's command/cwd/timeout and run it via run_command."""
    cmd = command if command is not None else step.command
    assert cmd is not None, "script step must have a command"
    return run_command(
        command=cmd, cwd=step.cwd or str(cwd),
        timeout_seconds=step.timeout_seconds, env_extra=env_extra,
        policy=policy, abort=abort, shim_dir=shim_dir)
