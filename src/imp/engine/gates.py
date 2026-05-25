"""Gate evaluation: decide block evaluator, script gate runner, human gate runner."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from imp.engine.types import Action, Decide, HumanGate, HumanOption, ScriptGate


class DecideNoMatch(Exception):
    """No decide rule matched and no else: true fallback present."""


def evaluate_decide(decide: Decide, context: dict[str, Any]) -> Action:
    """Return the first matching rule's action. Multi-field matches AND.

    Raise DecideNoMatch if nothing matched and no else: true fallback exists.
    """
    for rule in decide.rules:
        if rule.is_else:
            return rule.action
        matched = True
        for key, expected in rule.when:
            if context.get(key) != expected:
                matched = False
                break
        if matched:
            return rule.action
    raise DecideNoMatch(f"no rule matched and no else: true; context={context}")


def run_script_gate(
    gate: ScriptGate, *, cwd: Path
) -> tuple[Action, dict[str, Any]]:
    """Run the script, capture exit/stdout/stderr, evaluate decide.

    The `cwd` argument is the Change's project root; the gate's own `cwd:`
    overrides if set.

    `shell=True` is intentional and not a security issue: workflow YAML is part
    of the trusted code base (snapshotted at imp new, gitted, peer-reviewed)
    and `command:` strings are explicit shell expressions. The Change author
    and the workflow author are the same trust principal in V0.
    """
    proc = subprocess.run(
        gate.command,
        shell=True,
        cwd=gate.cwd or str(cwd),
        capture_output=True,
        text=True,
        timeout=gate.timeout_seconds,
        check=False,
    )
    context = {
        "exit": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    return evaluate_decide(gate.decide, context), context


def run_human_gate(
    gate: HumanGate,
    *,
    input_fn: Callable[[], str],
    output_fn: Callable[[str], None],
) -> Action:
    """Prompt the user with the gate's options. Returns the chosen action.

    Matching rules:
      - Numeric input -> 1-based option index
      - Otherwise case-insensitive label prefix match
      - Empty input or ambiguous prefix -> re-prompt
    """
    while True:
        output_fn("─── HUMAN GATE ──────────────────────────────────────")
        output_fn(gate.prompt)
        for i, opt in enumerate(gate.options, start=1):
            output_fn(f"  {i}) {opt.label}")
        output_fn("")
        raw = input_fn().strip()
        match = _match_human_option(raw, gate.options, output_fn)
        if match is not None:
            return match.action


def _match_human_option(
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
