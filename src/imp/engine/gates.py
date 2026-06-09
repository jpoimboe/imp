"""Step-body helpers: run a script step, evaluate a rules block."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.env import overlay_engine_env
from imp.engine.expr import EvalEnv, evaluate
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
) -> dict[str, Any]:
    """Run a script step; return {exit, stdout, stderr} for rule evaluation.

    `shell=True` is intentional and not a security issue: `command:` strings are
    explicit shell expressions the engine reads live from the Change's own
    worktree working tree (an uncommitted edit is honored on resume). The trust
    is principal-based, not snapshot-based: the Change author and the workflow
    author are the same trust principal in V0, so executing the worktree's
    (possibly uncommitted) command bytes grants no privilege the Change author
    doesn't already have.

    `env_extra` (an `engine_env` result) is overlaid authoritatively on the current
    process environment so engine-level variables (e.g. IMP_CHANGE_SLUG) are visible
    to scripts, while a stale inherited engine var (e.g. IMP_LINT_COMMAND from an
    outer imp process) cannot leak past engine_env's "absent => no gate" contract.
    """
    # The loader only constructs script steps with a non-None command; this
    # assert is purely for mypy (command: str | None in Step's type).
    assert step.command is not None, "script step must have a command"
    env = overlay_engine_env(os.environ, env_extra or {})
    proc = subprocess.run(
        step.command, shell=True, cwd=step.cwd or str(cwd),
        capture_output=True, text=True, timeout=step.timeout_seconds,
        check=False, env=env,
    )
    return {"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
