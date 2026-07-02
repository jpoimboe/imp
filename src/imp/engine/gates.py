"""Step-body helpers: run a script step, evaluate a rules block."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.env import overlay_engine_env
from imp.engine.expr import EvalEnv, evaluate
from imp.engine.sandbox import SandboxPolicy, build_sandbox_argv
from imp.engine.types import Action, Continue, Rule, Step

GATE_KILL_GRACE_S = 2.0  # SIGTERM-to-SIGKILL grace window for a gate subprocess group
_POLL_INTERVAL_S = 0.5  # max interrupt-to-kill latency: one poll tick


class ScriptInterrupted(Exception):
    """Raised by run_script when its abort predicate fires mid-run. The process
    group has already been torn down gracefully; the caller treats this as a
    pre-navigation pause, not a gate verdict."""


def _graceful_kill(proc: subprocess.Popen[str]) -> None:
    """Tear down a gate subprocess GROUP gracefully, then close its read FDs.

    SIGTERM the group (with start_new_session=True, proc.pid is the group
    leader, so this reaches the whole bwrap -> sh -> make -> pytest -> xdist
    tree), wait GATE_KILL_GRACE_S for it to flush and exit, SIGKILL any
    survivors, then reap. The final proc.wait() cannot deadlock on a full pipe:
    a SIGKILLed process is already dead, so wait returns regardless of unread
    buffer. Closing proc.stdout/proc.stderr releases the parent-side read FDs
    that a timed-out or aborted communicate() leaves open."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=GATE_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
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


def run_script(
    step: Step, *, cwd: Path, env_extra: dict[str, str] | None = None,
    policy: SandboxPolicy | None = None, command: str | None = None,
    abort: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a script step; return {exit, stdout, stderr} for rule evaluation.

    The command string is an explicit shell expression read live from the
    Change's own worktree (principal-based trust: the Change author and workflow
    author are the same V0 principal). When `policy` is active it is wrapped with
    bwrap (build_sandbox_argv appends `sh -c <command>`); otherwise the bare
    `sh -c` runs.

    Reaping (spec B.9/H-A): because the sandbox drops --die-with-parent, a timeout
    must kill the process GROUP, not just the bwrap pid — else the inner `sh` is
    orphaned. So this uses Popen(start_new_session=True) + a poll loop of
    communicate(timeout=poll) calls and a graceful group kill (SIGTERM -> grace ->
    SIGKILL) on both the timeout and abort paths. A gate's cwd (step.cwd or cwd)
    must lie inside the writable set (worktree/`/tmp`); a gate pointed at the RO
    floor fails.

    `env_extra` (an engine_env result) is overlaid authoritatively so engine vars
    are visible while a stale inherited engine var cannot leak (engine_env's
    "absent => no gate" contract).
    """
    cmd = command if command is not None else step.command
    assert cmd is not None, "script step must have a command"
    env = overlay_engine_env(os.environ, env_extra or {})
    run_cwd = step.cwd or str(cwd)
    # The policy's setenv was frozen at run start (possibly with an empty
    # name-on-intent slug), so the live engine_env (env_extra) is the authoritative
    # source that refreshes IMP_* inside the bwrap --clearenv jail.
    argv = (build_sandbox_argv(cmd, cwd=run_cwd, policy=policy,
                               engine_env=env_extra or {})
            if policy is not None else ["sh", "-c", cmd])
    proc = subprocess.Popen(
        argv, cwd=run_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    # Drain-preserving poll loop: each tick is a communicate() call (NOT
    # wait()/poll()), which is what reads stdout/stderr — so a chatty child never
    # deadlocks on a full pipe. CPython accumulates output across repeated
    # communicate(timeout=...) retries without loss. On each caught timeout,
    # check the abort predicate (interruptible steps only) and the overall
    # elapsed timeout; both terminal paths tear the group down gracefully.
    start = time.monotonic()
    timeout = step.timeout_seconds
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_POLL_INTERVAL_S)
        except subprocess.TimeoutExpired:
            if abort is not None and abort():
                _graceful_kill(proc)
                raise ScriptInterrupted from None
            if timeout is not None and time.monotonic() - start >= timeout:
                _graceful_kill(proc)
                raise subprocess.TimeoutExpired(proc.args, timeout) from None
            continue
        return {"exit": proc.returncode, "stdout": stdout.rstrip(), "stderr": stderr}
