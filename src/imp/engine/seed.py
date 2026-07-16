"""Shared quest-start seeding: the single home for stamping a State's cursor and
vars from a loaded workflow's first step, so the eager and deferred paths can't drift."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from imp.engine.state import State
    from imp.engine.types import Workflow
    from imp.providers.config import Config


def seed_workflow_start(state: State, workflow: Workflow, config: Config) -> None:
    """Stamp `state`'s cursor (step, step_label, phase_label) and vars from
    `workflow`'s first flat step, mutating in place. An empty workflow (no steps)
    leaves the cursor null; vars always get the workflow defaults with config
    overrides folded on top."""
    from imp.agents.prompts import render_label
    from imp.providers.config import apply_var_overrides

    flat = workflow.flat_steps()
    if flat:
        first = flat[0]
        state.step = first.name
        state.step_label = render_label(first.label, quest=None, log=None, state=None)
        state.phase_label = (
            render_label(first.phase_label, quest=None, log=None, state=None)
            if first.phase_label is not None else None
        )
    else:
        state.step = None
        state.step_label = None
        state.phase_label = None
    state.vars = apply_var_overrides(workflow.vars, config.var_overrides)
