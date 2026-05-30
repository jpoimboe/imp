"""Step-body helpers: run a script step, run a human step, evaluate a rules block."""
from __future__ import annotations

import inspect
import os
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.expr import EvalEnv, evaluate
from imp.engine.types import Action, Continue, HumanOption, Rule, Step


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


def apply_human_option(option: HumanOption, *, vars: dict[str, Any]) -> dict[str, object]:
    env = EvalEnv(vars=vars, output=None)
    return {name: evaluate(expr, env) for name, expr in option.mutations}


def run_script(
    step: Step, *, cwd: Path, env_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a script step; return {exit, stdout, stderr} for rule evaluation.

    `shell=True` is intentional and not a security issue: workflow YAML is part
    of the trusted code base (snapshotted at imp new, gitted, peer-reviewed)
    and `command:` strings are explicit shell expressions. The Change author
    and the workflow author are the same trust principal in V0.

    `env_extra` is merged on top of the current process environment so that
    engine-level variables (e.g. IMP_ARTIFACTS_DIR) are visible to scripts.
    """
    # The loader only constructs script steps with a non-None command; this
    # assert is purely for mypy (command: str | None in Step's type).
    assert step.command is not None, "script step must have a command"
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        step.command, shell=True, cwd=step.cwd or str(cwd),
        capture_output=True, text=True, timeout=step.timeout_seconds,
        check=False, env=env,
    )
    return {"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


async def run_human(
    step: Step, *, input_fn: Callable[[], str | Awaitable[str]],
    output_fn: Callable[[str], None],
) -> HumanOption:
    """Prompt with the step's options; return the chosen HumanOption (carries
    the action verb + set:)."""
    while True:
        output_fn("--- HUMAN ---------------------------------------------------")
        output_fn(step.ask or "")
        for i, opt in enumerate(step.options, start=1):
            output_fn(f"  {i}) {opt.label}")
        output_fn("")
        result = input_fn()
        if inspect.isawaitable(result):
            result = await result
        chosen = _match_option(result.strip(), step.options, output_fn)
        if chosen is not None:
            return chosen


def _match_option(
    raw: str, options: tuple[HumanOption, ...], output_fn: Callable[[str], None]
) -> HumanOption | None:
    if not raw:
        output_fn("  (empty input; try again)")
        return None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]
        output_fn(f"  (no option {raw}; try again)")
        return None
    needle = raw.lower()
    # Exact match takes priority over prefix matches
    exact = [o for o in options if o.label.lower() == needle]
    if len(exact) == 1:
        return exact[0]
    candidates = [o for o in options if o.label.lower().startswith(needle)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        labels = ", ".join(c.label for c in candidates)
        output_fn(f"  (ambiguous; matches: {labels})")
        return None
    output_fn(f"  (no match for {raw!r}; try again)")
    return None
