"""Apply a per-step model override into run state (spec: /model, --model)."""
from __future__ import annotations

from typing import Any, cast

from imp.engine.loader import load_workflow
from imp.engine.state import State
from imp.providers.config import ProviderModel
from imp.providers.model_arg import parse_model_arg
from imp.providers.resolve import resolve_run


def current_provider_model(state: Any, workflow: Any, step_name: str) -> ProviderModel:
    steps = state.resolved_steps or {}
    if step_name in steps:
        return cast(ProviderModel, steps[step_name])
    step = next(s for s in workflow.flat_steps() if s.name == step_name)
    assert state.resolved_tiers is not None
    return cast(ProviderModel, state.resolved_tiers[step.tier])


def resolve_current_model(
    state: Any, workflow: Any, config: Any, step_name: str,
) -> ProviderModel:
    """step_name's current model: override, else tier default; total when unresolved."""
    steps = state.resolved_steps or {}
    if step_name in steps:
        return cast(ProviderModel, steps[step_name])
    tiers = state.resolved_tiers
    if tiers is None:
        tiers, _, _ = resolve_run(workflow, config)
    step = next(s for s in workflow.flat_steps() if s.name == step_name)
    return cast(ProviderModel, tiers[step.tier])


def apply_model_override(
    change: Any, state: Any, arg: str, *, step_name: str,
) -> None:
    """Resolve `arg` against `step_name`'s current provider and persist the override."""
    parsed = parse_model_arg(arg)
    workflow = load_workflow(
        change.workflow_dir,
        project_keys=frozenset(state.project_facts or {}),
    )
    current = current_provider_model(state, workflow, step_name)
    provider = parsed.provider if parsed.provider is not None else current.provider
    if state.resolved_steps is None:
        state.resolved_steps = {}
    state.resolved_steps[step_name] = ProviderModel(
        provider=provider, model=parsed.model)
    State.save(state, change.state_dir)
