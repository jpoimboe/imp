"""Step-body helpers: run a script step, evaluate a rules block."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.env import overlay_engine_env
from imp.engine.expr import EvalEnv, evaluate
from imp.engine.sandbox import SandboxPolicy, build_sandbox_argv
from imp.engine.types import Action, Continue, Rule, Step


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
    policy: SandboxPolicy | None = None,
) -> dict[str, Any]:
    """Run a script step; return {exit, stdout, stderr} for rule evaluation.

    The command string is an explicit shell expression read live from the
    Change's own worktree (principal-based trust: the Change author and workflow
    author are the same V0 principal). When `policy` is active it is wrapped with
    bwrap (build_sandbox_argv appends `sh -c <command>`); otherwise the bare
    `sh -c` runs.

    Reaping (spec B.9/H-A): because the sandbox drops --die-with-parent, a timeout
    must kill the process GROUP, not just the bwrap pid — else the inner `sh` is
    orphaned. So this uses Popen(start_new_session=True) + communicate(timeout) and
    killpg(SIGKILL) on TimeoutExpired. A gate's cwd (step.cwd or cwd) must lie
    inside the writable set (worktree/`/tmp`); a gate pointed at the RO floor fails.

    `env_extra` (an engine_env result) is overlaid authoritatively so engine vars
    are visible while a stale inherited engine var cannot leak (engine_env's
    "absent => no gate" contract).
    """
    assert step.command is not None, "script step must have a command"
    env = overlay_engine_env(os.environ, env_extra or {})
    run_cwd = step.cwd or str(cwd)
    argv = (build_sandbox_argv(step.command, cwd=run_cwd, policy=policy)
            if policy is not None else ["sh", "-c", step.command])
    proc = subprocess.Popen(
        argv, cwd=run_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=step.timeout_seconds)
    except subprocess.TimeoutExpired:
        # H-A: --die-with-parent is dropped, so kill the GROUP to reap the inner sh.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        # communicate() leaves the parent-side read FDs open when it times out;
        # close them so a timed-out gate doesn't leak two FDs until GC.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        raise
    return {"exit": proc.returncode, "stdout": stdout, "stderr": stderr}
