# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Validate a /model or --model argument at the point of entry."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from imp.providers.config import ProviderModel
from imp.providers.model_arg import ModelArgError, parse_model_arg
from imp.providers.model_roster import ProviderRoster, group_models

# Hidden from the roster but still resolvable; exact-name match keeps dated ids listed.
_HIDDEN_ALIASES = frozenset({"haiku", "sonnet", "opus", "gpt-5.6"})


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
        marked = None
        groups = roster.groups
        if roster.provider == current.provider:
            marked = current.model
            groups = group_models(roster.provider, (*roster.names, marked))
        for group in groups:
            rows = [n for n in group.models if n == marked or n not in _HIDDEN_ALIASES]
            if rows:
                lines.append(f"    {group.name}")
            lines.extend(f"      {n}   current" if n == marked else f"      {n}" for n in rows)
    lines.append("")
    lines.append("Switch with  /model <name>  or  /model provider:model")
    return lines


def print_model_roster(
    *, quest: Any, output_fn: Callable[[str], None],
) -> None:
    """Bare /model: print the current step's model and the discovered roster."""
    from imp.engine.loader import load_workflow
    from imp.engine.model_override import resolve_current_model
    from imp.engine.state import State
    from imp.providers.claude_model_cache import read_claude_model_options
    from imp.providers.effective import load_effective_config
    from imp.providers.model_roster import discover_roster

    state = State.load(quest.state_dir)
    config = load_effective_config(control_root=quest.control_root).config
    workflow = load_workflow(quest.workflow_dir)
    assert state.step is not None
    current = resolve_current_model(state, workflow, config, state.step)
    rosters = discover_roster(
        config, claude_options=read_claude_model_options())
    for line in render_roster(current, rosters):
        output_fn(line)


def roster_model_names(quest: Any) -> list[str]:
    """Flat, de-duplicated model-name candidates (aliases incl.) for /model completion."""
    from imp.providers.claude_model_cache import read_claude_model_options
    from imp.providers.effective import load_effective_config
    from imp.providers.model_roster import discover_roster

    config = load_effective_config(control_root=quest.control_root).config
    rosters = discover_roster(config, claude_options=read_claude_model_options())
    names: list[str] = []
    for roster in rosters:
        for name in roster.names:
            if name not in names:
                names.append(name)
    return names


def _available_for_quest(quest: Any) -> list[str]:
    from imp.providers.effective import load_effective_config
    from imp.providers.resolve import available_providers
    config = load_effective_config(control_root=quest.control_root).config
    return available_providers(config)


def validate_model_available(arg: str, *, available: list[str]) -> str | None:
    provider = parse_model_arg(arg).provider
    if provider is not None and provider not in available:
        return f"provider {provider!r} is not available — nothing changed"
    return None


def validate_model_arg(arg: str, *, quest: Any) -> str | None:
    try:
        parse_model_arg(arg)
    except ModelArgError as exc:
        return str(exc)
    return validate_model_available(arg, available=_available_for_quest(quest))
