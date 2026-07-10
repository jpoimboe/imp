"""Validate a /model or --model argument at the point of entry."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from imp.providers.config import ProviderModel
from imp.providers.model_arg import ModelArgError, parse_model_arg
from imp.providers.model_roster import ProviderRoster


def render_roster(
    current: ProviderModel, rosters: list[ProviderRoster],
) -> list[str]:
    """Format bare /model: current model, per-provider roster, and switch hint. Pure."""
    lines = [
        f"Current: {current.provider} · {current.model}   (this step)",
        "",
        "Available:",
    ]
    for roster in rosters:
        lines.append(f"  {roster.provider}")
        plain = [e.name for e in roster.entries if e.description is None]
        if plain:
            lines.append("    " + " · ".join(plain))
        for entry in roster.entries:
            if entry.description is not None:
                lines.append(f"    {entry.name}    {entry.description}")
    lines.append("")
    lines.append("Switch with  /model <name>  or  /model provider:model")
    return lines


def print_model_roster(
    *, change: Any, output_fn: Callable[[str], None],
) -> None:
    """Bare /model: print the current step's model and the discovered roster."""
    from imp.engine.loader import load_workflow
    from imp.engine.model_override import resolve_current_model
    from imp.engine.state import State
    from imp.providers.claude_model_cache import read_claude_model_options
    from imp.providers.effective import load_effective_config
    from imp.providers.model_roster import discover_roster

    state = State.load(change.state_dir)
    config = load_effective_config(
        project_root=change.project_root, control_root=change.control_root,
    ).config
    workflow = load_workflow(
        change.workflow_dir,
        project_keys=frozenset(state.project_facts or {}),
    )
    assert state.step is not None
    current = resolve_current_model(state, workflow, config, state.step)
    rosters = discover_roster(
        config, claude_options=read_claude_model_options())
    for line in render_roster(current, rosters):
        output_fn(line)


def _available_for_change(change: Any) -> list[str]:
    from imp.providers.effective import load_effective_config
    from imp.providers.resolve import available_providers
    config = load_effective_config(
        project_root=change.project_root, control_root=change.control_root,
    ).config
    return available_providers(config)


def validate_model_available(arg: str, *, available: list[str]) -> str | None:
    provider = parse_model_arg(arg).provider
    if provider is not None and provider not in available:
        return f"provider {provider!r} is not available — nothing changed"
    return None


def validate_model_arg(arg: str, *, change: Any) -> str | None:
    try:
        parse_model_arg(arg)
    except ModelArgError as exc:
        return str(exc)
    return validate_model_available(arg, available=_available_for_change(change))
