"""Workflow executor: step, phase, top-level loops; action handling."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel

from imp.agents.prompts import PromptRenderError, build_context, render_prompt
from imp.agents.runner import AgentInvocation, AgentRunner
from imp.engine.change import Change
from imp.engine.gates import run_human_gate, run_script_gate
from imp.engine.loader import load_workflow
from imp.engine.schema import validate_agent_output
from imp.engine.state import EventLog, State
from imp.engine.types import (
    Action,
    AgentDef,
    AgentGate,
    Continue,
    Goto,
    HumanGate,
    Pause,
    Phase,
    Retry,
    ScriptGate,
    Step,
    StubGate,
    Workflow,
)
from imp.providers.config import Config, load_config
from imp.providers.resolve import ProviderUnavailable, resolve_role

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


def _open_checkpointer(state_dir: Path) -> Any:
    """Return a SqliteSaver context manager for state_dir/checkpoint.db.

    Returns whatever shape SqliteSaver.from_conn_string returns (context
    manager in current versions). Caller uses `with ... as checkpointer:`.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    state_dir.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(state_dir / "checkpoint.db"))


class RunResult(Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    ERRORED = "errored"


def execute_step(
    step: Step,
    agent_def: AgentDef,
    change: Change,
    runner: AgentRunner,
    state: State,
    *,
    model: BaseChatModel | None = None,
    events_path: Path | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> tuple[Action, dict[str, Any] | None]:
    """Run one step. Returns (action, agent_output_or_None).

    Caller supplies `agent_def` (looked up from `workflow.agents`) — execute_step
    does not re-parse the agent file. V14 guarantees workflow.agents[step.file.stem]
    is the right def.
    """
    log = change.event_log()

    # Resume-skip-step (Plan 1 review #3 + Plan 2 reviews #7 #8):
    # If we have a cached agent_output for this (phase, step) from a prior
    # run, skip the runner and re-evaluate gates against the cached output.
    # Guarded by step_attempt == 0 so a Retry from a gate doesn't loop
    # forever against the same cache.
    cached = (
        log.cached_output_for(state.phase, step.name)
        if state.step_attempt == 0 else None
    )
    if cached is not None:
        log.append("step_resumed_with_cached_output", {
            "step": step.name, "phase": state.phase,
        })
        # Schema-validate the cache (defense in depth)
        try:
            validate_agent_output(cached, agent_def.output_schema)
        except Exception as e:
            log.append("error", {
                "message": f"cached output failed schema validation: {e}",
            })
            log.append("step_completed", {
                "step": step.name, "action": "retry",
                "reason": "cached output invalid; rerunning",
            })
            return Retry(), None
        action = _run_step_gates(
            step, change, log, cached,
            runner=runner, resolved_models=None,
            checkpointer=checkpointer, workflow=None,
        )
        log.append("step_completed", {
            "step": step.name, "action": _action_name(action),
        })
        return action, cached

    try:
        rendered_prompt = render_prompt(
            agent_def.prompt_template_source,
            build_context(
                change_id=change.id, change_title=change.title,
                artifacts_dir=change.artifacts_dir,
                event_log=log,
            ),
        )
    except PromptRenderError as e:
        log.append("error", {"message": f"prompt render failed: {e}"})
        log.append("step_started", {
            "step": step.name, "phase": state.phase,
            "model_role": step.model_role, "interactive": step.interactive,
        })
        log.append("step_completed", {
            "step": step.name, "action": "retry",
            "reason": "prompt render failed",
        })
        return Retry(), None

    log.append("step_started", {
        "step": step.name,
        "phase": state.phase,
        "model_role": step.model_role,
        "interactive": step.interactive,
    })

    invocation = AgentInvocation(
        agent_def=agent_def,
        model_role=step.model_role,
        change_id=change.id,
        change_artifacts_dir=change.artifacts_dir,
        project_root=change.project_root,
        interactive=step.interactive,
        model=model,
        system_prompt=rendered_prompt,
        initial_messages=({"role": "user", "content": "begin"},),
        events_path=events_path or change.events_path,
        checkpointer=checkpointer,
    )

    agent_output: dict[str, Any] | None = None
    runner_failed = False
    # Runner crashes become an `error` event + Retry instead of propagating up.
    try:
        for event in runner.run(invocation):
            log.append(event["type"], event["payload"])
            if event["type"] == "agent_output":
                agent_output = event["payload"]
            if event["type"] == "error":
                runner_failed = True
    except Exception as e:
        log.append(
            "error",
            {"message": f"agent runner crashed: {type(e).__name__}: {e}"},
        )
        runner_failed = True

    if runner_failed or agent_output is None:
        log.append("step_completed", {
            "step": step.name, "action": "retry",
            "reason": "agent did not produce valid output",
        })
        return Retry(), None
    try:
        validate_agent_output(agent_output, agent_def.output_schema)
    except Exception as e:
        log.append(
            "error",
            {"message": f"agent output failed schema validation: {e}"},
        )
        log.append("step_completed", {
            "step": step.name, "action": "retry",
            "reason": "schema validation failed",
        })
        return Retry(), None

    action = _run_step_gates(
        step, change, log, agent_output,
        runner=None, resolved_models=None,
        checkpointer=checkpointer, workflow=None,
    )
    log.append("step_completed", {"step": step.name, "action": _action_name(action)})
    return action, agent_output


def execute_phase(
    phase: Phase,
    workflow: Workflow,
    change: Change,
    runner: AgentRunner,
    state: State,
    *,
    resolved_models: dict[str, BaseChatModel] | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Action:
    """Walk phase steps, then phase gates. `workflow` is needed for AgentDef
    lookup keyed by `step.file.stem` (V14 ensures the stem == agent name).
    """
    log = change.event_log()
    log.append("phase_started", {"phase": phase.name})

    # Resume support: skip steps before the cursor.
    start_idx = 0
    if state.step:
        for i, st in enumerate(phase.steps):
            if st.name == state.step:
                start_idx = i
                break

    i = start_idx
    while i < len(phase.steps):
        step = phase.steps[i]
        state.step = step.name
        state.save(change.state_dir)
        action, _ = execute_step(
            step=step,
            agent_def=workflow.agents[step.file.stem],
            change=change,
            runner=runner,
            state=state,
            model=(resolved_models.get(step.model_role) if resolved_models else None),
            events_path=change.events_path,
            checkpointer=checkpointer,
        )
        if isinstance(action, Continue):
            i += 1
            state.step_attempt = 0
        elif isinstance(action, Retry):
            state.step_attempt += 1
            if state.step_attempt > step.max_retries:
                log.append("step_exhausted", {
                    "step": step.name, "attempts": state.step_attempt,
                })
                state.step_attempt = 0
                return step.on_max_retries
            continue
        else:
            log.append("phase_completed", {
                "phase": phase.name, "action": _action_name(action),
            })
            return action

    # All steps done; now run phase gates.
    for gate in phase.gates:
        action = _run_one_gate(
            gate, change, log, agent_output={},
            runner=runner, resolved_models=resolved_models,
            checkpointer=checkpointer, workflow=workflow,
        )
        log.append("gate_evaluated", {
            "phase": phase.name, "gate_type": type(gate).__name__,
            "action": _action_name(action),
        })
        if not isinstance(action, Continue):
            log.append("phase_completed", {
                "phase": phase.name, "action": _action_name(action),
            })
            return action

    log.append("phase_completed", {"phase": phase.name, "action": "continue"})
    return Continue()


def _resolve_all_referenced_roles(
    workflow: Workflow, config: Config,
) -> dict[str, BaseChatModel]:
    """Walk every step + agent gate, collect model_role refs, resolve each.
    All failures surfaced together."""
    referenced: set[str] = set()
    for phase in workflow.phases:
        for step in phase.steps:
            referenced.add(step.model_role)
        for gate in phase.gates:
            if isinstance(gate, AgentGate):
                referenced.add(gate.model_role)

    resolved: dict[str, BaseChatModel] = {}
    failures: list[str] = []
    for role_name in sorted(referenced):
        if role_name not in config.roles:
            failures.append(
                f"role {role_name!r} referenced by workflow but not in "
                f"config.yaml (configured: {sorted(config.roles)})"
            )
            continue
        try:
            resolved[role_name] = resolve_role(config.roles[role_name])
        except Exception as e:
            failures.append(str(e))

    if failures:
        raise ProviderUnavailable(
            "startup model resolution failed:\n  - "
            + "\n  - ".join(failures)
        )
    return resolved


def run(change: Change, runner: AgentRunner) -> RunResult:
    """Top-level executor: walk phases until done, paused, or errored.

    Retry-counter semantics:
      `max_retries: N` allows 1 + N total attempts (1 initial + N retries).
      The comparison is `attempt > max_retries`, so for max_retries=2, attempts
      run 1, 2, 3 before `on_max_retries` fires.
    """
    # Load config so V12 (model_role configured) fires for the snapshotted workflow.
    config = load_config(change.project_root)
    configured_roles = config.role_names()
    workflow = load_workflow(
        change.workflow_dir, configured_roles=configured_roles
    )
    resolved_models = _resolve_all_referenced_roles(workflow, config)
    state = State.load(change.state_dir)
    log = change.event_log()
    log.append(
        "workflow_started",
        {"name": workflow.name, "from_phase": state.phase},
    )

    with _open_checkpointer(change.state_dir) as checkpointer:
        while True:
            if state.phase is None:
                state.status = "completed"
                state.save(change.state_dir)
                log.append("workflow_completed", {})
                return RunResult.COMPLETED

            phase_idx = workflow.phase_index(state.phase)
            if phase_idx is None:
                log.append(
                    "error",
                    {"message": f"unknown phase {state.phase!r} in state"},
                )
                state.status = "errored"
                state.save(change.state_dir)
                return RunResult.ERRORED
            phase = workflow.phases[phase_idx]

            action = execute_phase(
                phase=phase, workflow=workflow, change=change,
                runner=runner, state=state,
                resolved_models=resolved_models,
                checkpointer=checkpointer,
            )

            if isinstance(action, Continue):
                next_name = workflow.next_phase(phase.name)
                state.phase = next_name
                state.step = None
                state.phase_attempt = 0
                state.save(change.state_dir)
            elif isinstance(action, Goto):
                state.phase = action.target
                state.step = None
                state.phase_attempt = 0
                state.save(change.state_dir)
            elif isinstance(action, Retry):
                state.phase_attempt += 1
                if state.phase_attempt > phase.max_retries:
                    log.append("phase_exhausted", {
                        "phase": phase.name, "attempts": state.phase_attempt,
                    })
                    state.phase_attempt = 0
                    on_max = phase.on_max_retries
                    if isinstance(on_max, Pause):
                        state.status = "paused"
                        state.save(change.state_dir)
                        log.append(
                            "workflow_paused",
                            {"reason": "phase_exhausted"},
                        )
                        return RunResult.PAUSED
                    elif isinstance(on_max, Goto):
                        state.phase = on_max.target
                        state.step = None
                        state.save(change.state_dir)
                    elif isinstance(on_max, Continue):
                        state.phase = workflow.next_phase(phase.name)
                        state.step = None
                        state.save(change.state_dir)
                else:
                    state.step = None
                    state.save(change.state_dir)
            elif isinstance(action, Pause):
                state.status = "paused"
                state.save(change.state_dir)
                log.append("workflow_paused", {"reason": "explicit"})
                return RunResult.PAUSED


def _run_step_gates(
    step: Step, change: Change, log: EventLog, agent_output: dict[str, Any],
    *,
    runner: AgentRunner | None = None,
    resolved_models: dict[str, BaseChatModel] | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    workflow: Workflow | None = None,
) -> Action:
    if not step.gates:
        # Implicit: continue if status: Success, retry on anything else.
        return Continue() if agent_output.get("status") == "Success" else Retry()
    for gate in step.gates:
        action = _run_one_gate(
            gate, change, log, agent_output,
            runner=runner, resolved_models=resolved_models,
            checkpointer=checkpointer, workflow=workflow,
        )
        log.append("gate_evaluated", {
            "step": step.name, "gate_type": type(gate).__name__,
            "action": _action_name(action),
        })
        if not isinstance(action, Continue):
            return action
    return Continue()


def _run_one_gate(
    gate: Any,
    change: Change,
    log: EventLog,
    agent_output: dict[str, Any],
    *,
    runner: AgentRunner | None = None,
    resolved_models: dict[str, BaseChatModel] | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    workflow: Workflow | None = None,
) -> Action:
    if isinstance(gate, ScriptGate):
        action, context = run_script_gate(gate, cwd=change.project_root)
        log.append("script_gate_result", context)
        return action
    if isinstance(gate, HumanGate):
        return run_human_gate(gate, input_fn=input, output_fn=print)
    if isinstance(gate, AgentGate):
        return _run_agent_gate(
            gate, change, log,
            runner=runner, resolved_models=resolved_models,
            checkpointer=checkpointer, workflow=workflow,
        )
    if isinstance(gate, StubGate):
        return gate.action
    raise AssertionError(f"unknown gate type {type(gate).__name__}")


def _run_agent_gate(
    gate: AgentGate,
    change: Change,
    log: EventLog,
    *,
    runner: AgentRunner | None,
    resolved_models: dict[str, BaseChatModel] | None,
    checkpointer: BaseCheckpointSaver[Any] | None,
    workflow: Workflow | None,
) -> Action:
    """Run an agent as a gate. Output is schema-validated, then fed to decide."""
    from imp.engine.gates import evaluate_decide

    assert runner is not None and resolved_models is not None and workflow is not None

    stem = gate.file.stem
    agent_def = workflow.agents[stem]  # loader pre-parses every agents/*.md

    try:
        rendered_prompt = render_prompt(
            agent_def.prompt_template_source,
            build_context(
                change_id=change.id, change_title=change.title,
                artifacts_dir=change.artifacts_dir,
                event_log=change.event_log(),
            ),
        )
    except PromptRenderError as e:
        log.append("error", {"message": f"gate prompt render failed: {e}"})
        return Pause()

    invocation = AgentInvocation(
        agent_def=agent_def,
        model_role=gate.model_role,
        change_id=change.id,
        change_artifacts_dir=change.artifacts_dir,
        project_root=change.project_root,
        interactive=False,
        model=resolved_models.get(gate.model_role),
        system_prompt=rendered_prompt,
        initial_messages=({"role": "user", "content": "begin"},),
        events_path=change.events_path,
        checkpointer=checkpointer,
    )
    log.append("step_started", {
        "step": f"gate:{stem}", "phase": None,
        "model_role": gate.model_role, "interactive": False,
    })
    output: dict[str, Any] | None = None
    for event in runner.run(invocation):
        log.append(event["type"], event["payload"])
        if event["type"] == "agent_output":
            output = event["payload"]
        if event["type"] == "error":
            log.append("step_completed", {
                "step": f"gate:{stem}", "action": "pause",
                "reason": "gate errored",
            })
            return Pause()
    if output is None:
        log.append("step_completed", {
            "step": f"gate:{stem}", "action": "pause",
            "reason": "gate produced no output",
        })
        return Pause()

    # Per review #9: schema-validate before evaluating decide.
    try:
        validate_agent_output(output, agent_def.output_schema)
    except Exception as e:
        log.append("error", {
            "message": f"gate output failed schema validation: {e}",
        })
        log.append("step_completed", {
            "step": f"gate:{stem}", "action": "pause",
            "reason": "schema validation failed",
        })
        return Pause()

    action = evaluate_decide(gate.decide, output)
    log.append("step_completed", {
        "step": f"gate:{stem}", "action": _action_name(action),
    })
    return action


def _action_name(action: Action) -> str:
    return type(action).__name__.lower()
