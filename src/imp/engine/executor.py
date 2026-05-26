"""Workflow executor (V1): async step dispatch, per-step transcript cleanup,
gate evaluation, state persistence.

The engine owns state.json + events.jsonl. The runner owns transcript.jsonl.
On successful step completion the engine deletes the step's transcript dir.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from enum import Enum
from typing import Any

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.engine.change import Change
from imp.engine.gates import (
    run_human_gate,
    run_script_gate,
)
from imp.engine.loader import load_workflow
from imp.engine.schema import validate_agent_output
from imp.engine.state import CurrentStep, EventLog, State
from imp.engine.types import (
    Action,
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
from imp.providers.resolve import resolve_role

EventCallback = Callable[[dict[str, Any]], None]
HumanInputFn = Callable[[], str]
HumanOutputFn = Callable[[str], None]


class RunResult(Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    ERRORED = "errored"


async def run_change(
    change: Change,
    *,
    on_event: EventCallback | None = None,
    human_input_fn: HumanInputFn = input,
    human_output_fn: HumanOutputFn = print,
    user_chat_input_fn: Callable[[str], str] | None = None,
    coordinator: Any = None,  # cli.signals.InterruptCoordinator | None
) -> RunResult:
    on_event = on_event or (lambda _e: None)
    if coordinator is None:
        # Lazy import to avoid circular dep at module-load time.
        from imp.cli.signals import InterruptCoordinator
        coordinator = InterruptCoordinator()

    workflow = load_workflow(change.workflow_dir)
    config = load_config(change.project_root)

    log = EventLog(change.events_path)
    state = State.load(change.state_dir)

    while True:
        if state.phase is None:
            state.status = "completed"
            state.save(change.state_dir)
            return RunResult.COMPLETED

        phase = next(p for p in workflow.phases if p.name == state.phase)
        action = await _execute_phase(
            phase, workflow, config, change, state, log, on_event,
            human_input_fn=human_input_fn, human_output_fn=human_output_fn,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
        )

        # Coordinator pause request takes precedence over the action's intent.
        if coordinator.should_pause:
            state.status = "paused"
            state.save(change.state_dir)
            _emit(log, on_event, "workflow_paused", {})
            return RunResult.PAUSED

        if isinstance(action, Pause):
            state.status = "paused"
            state.save(change.state_dir)
            _emit(log, on_event, "workflow_paused", {})
            return RunResult.PAUSED
        if isinstance(action, Continue):
            next_phase = _next_phase(workflow, state.phase)
            state.phase = next_phase
            state.step = None
            state.step_attempt = 0
            state.phase_attempt = 0
        elif isinstance(action, Goto):
            state.phase = action.target
            state.step = None
            state.step_attempt = 0
            state.phase_attempt = 0
        else:
            raise AssertionError(f"unexpected action {action!r}")
        state.save(change.state_dir)


def _next_phase(workflow: Workflow, current: str) -> str | None:
    """Return the name of the phase after `current`, or None if last."""
    names = [p.name for p in workflow.phases]
    idx = names.index(current)
    if idx + 1 >= len(names):
        return None
    return names[idx + 1]


async def _execute_phase(
    phase: Phase, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, on_event: EventCallback,
    *,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str] | None,
    coordinator: Any,
) -> Action:
    _emit(log, on_event, "phase_started", {"phase": phase.name})
    # Outer loop: each iteration is one walk of the steps; step-level retry
    # bounces back to the top via `continue`. State mutations carry across
    # iterations — no recursion.
    while True:
        steps_iter = list(phase.steps)
        start_idx = 0
        if state.step is not None:
            for i, s in enumerate(steps_iter):
                if s.name == state.step:
                    start_idx = i
                    break

        retry_from_top = False
        for step in steps_iter[start_idx:]:
            state.step = step.name
            state.save(change.state_dir)
            action = await _execute_step(
                step, workflow, config, change, state, log, on_event,
                human_input_fn=human_input_fn,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
            )
            if isinstance(action, Retry):
                state.step_attempt += 1
                if state.step_attempt > step.max_retries:
                    action = step.on_max_retries
                    # Validator forbids on_max_retries == Retry. Defense-in-depth:
                    # if a malformed workflow slipped past validation, fail loud.
                    assert not isinstance(action, Retry), (
                        f"on_max_retries cannot be Retry; "
                        f"got {action!r} for step {step.name!r}"
                    )
                else:
                    # Restart the steps walk from this same step.
                    retry_from_top = True
                    break
            if isinstance(action, (Goto, Pause)):
                return action
            if isinstance(action, Continue):
                state.step_attempt = 0
                continue

        if retry_from_top:
            continue  # outer while True: re-enter the steps walk

        # Phase-level gates chain: each Continue means "next gate"; only the
        # last gate's Continue ends the phase.
        retried_phase = False
        for gate in phase.gates:
            action = await _run_gate(
                gate, workflow, config, change, state, log, on_event,
                human_input_fn=human_input_fn, human_output_fn=human_output_fn,
                user_chat_input_fn=user_chat_input_fn,
            )
            if isinstance(action, Retry):
                state.phase_attempt += 1
                if state.phase_attempt > phase.max_retries:
                    action = phase.on_max_retries
                if isinstance(action, Retry):
                    state.step = None
                    state.step_attempt = 0
                    retried_phase = True
                    break  # re-enter the outer steps walk
            if isinstance(action, (Goto, Pause)):
                return action
            # Continue: proceed to next gate (no early return)
        if retried_phase:
            continue  # outer while True: re-walk steps then gates
        _emit(log, on_event, "phase_completed", {"phase": phase.name})
        return Continue()


async def _execute_step(
    step: Step, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, on_event: EventCallback,
    *,
    human_input_fn: HumanInputFn,
    user_chat_input_fn: Callable[[str], str] | None,
    coordinator: Any,
) -> Action:
    agent_def = workflow.agents[step.file.stem]
    role = config.roles.get(step.model_role) or config.roles["default"]
    provider = resolve_role(role)

    artifacts_dir = change.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    assert state.phase is not None, "phase must be set before executing a step"
    transcript_dir = change.change_dir / "steps" / state.phase / step.name
    transcript_dir.mkdir(parents=True, exist_ok=True)

    rendered = render_prompt(
        agent_def.prompt_template_source, build_context(
            change_id=change.id, change_title=change.title,
            artifacts_dir=artifacts_dir, event_log=log,
        ),
    )

    # Resolve subagents into their AgentDefs
    subagent_defs = {
        name: workflow.agents[name]
        for name in agent_def.subagents
        if name in workflow.agents
    }

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)

    # Spec §6.5 ceiling resolution: agent.max_tool_calls → defaults → 50.
    # Use `is not None` (not `or`) so a literal 0 doesn't silently fall through.
    if agent_def.max_tool_calls is not None:
        resolved_max_tool_calls = agent_def.max_tool_calls
    elif workflow.defaults.max_tool_calls is not None:
        resolved_max_tool_calls = workflow.defaults.max_tool_calls
    else:
        resolved_max_tool_calls = 50

    invocation = AgentInvocation(
        agent_def=agent_def,
        provider_name=provider.name,
        model_name=provider.model,
        system_prompt=rendered,
        initial_messages=(Message(role="user", content=(TextBlock(text="begin"),)),),
        change_context=ChangeContext(
            change_id=change.id, project_root=change.project_root, artifacts_dir=artifacts_dir,
        ),
        interactive=step.interactive,
        transcript_dir=transcript_dir,
        fs_permissions=step.fs_permissions,
        events_path=change.events_path,
        subagent_defs=subagent_defs,
        max_tool_calls=resolved_max_tool_calls,
    )

    _emit(log, on_event, "step_started", {
        "step": step.name, "model": invocation.model_name,
        "provider": provider.name,
        "tools": sorted(agent_def.tools),
        "interactive": step.interactive,
    })

    runner = provider.create_runner(invocation)
    agent_output: dict[str, Any] | None = None

    coordinator.attach_runner(runner.interrupt)
    try:
        async for event in runner.run(invocation):
            _emit_event(log, on_event, event)
            if event["type"] == "agent_output":
                agent_output = event["payload"]
            if event["type"] == "error":
                _emit(log, on_event, "step_completed", {"step": step.name, "action": "retry"})
                return Retry()
            if coordinator.should_pause:
                # Coordinator triggered mid-step. Runner's interrupt() has fired.
                return Pause()
    finally:
        coordinator.detach_runner()

    if agent_output is None:
        _emit(log, on_event, "step_completed", {"step": step.name, "action": "retry"})
        return Retry()

    try:
        validate_agent_output(agent_output, agent_def.output_schema)
    except Exception as e:
        _emit(log, on_event, "error", {"message": f"output validation: {e}"})
        return Retry()

    # Evaluate step-level gates
    for gate in step.gates:
        action = await _run_gate(
            gate, workflow, config, change, state, log, on_event,
            human_input_fn=human_input_fn, human_output_fn=print,
            user_chat_input_fn=user_chat_input_fn,
            agent_output_context=agent_output,
        )
        if not isinstance(action, Continue):
            return action

    # Step success — delete transcript dir
    shutil.rmtree(transcript_dir, ignore_errors=True)
    state.current_step = None
    state.save(change.state_dir)
    _emit(log, on_event, "step_completed", {"step": step.name, "action": "continue"})
    return Continue()


async def _run_gate(
    gate: Any, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, on_event: EventCallback,
    *,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str] | None,
    agent_output_context: dict[str, Any] | None = None,
) -> Action:
    if isinstance(gate, HumanGate):
        action = run_human_gate(gate, input_fn=human_input_fn, output_fn=human_output_fn)
        _emit(log, on_event, "gate_evaluated", {"kind": "human", "action": _action_dict(action)})
        return action
    if isinstance(gate, ScriptGate):
        action, ctx = run_script_gate(gate, cwd=change.project_root)
        _emit(log, on_event, "gate_evaluated",
              {"kind": "script", "context": ctx, "action": _action_dict(action)})
        return action
    if isinstance(gate, AgentGate):
        # Agent gates land in Plan 3.
        raise NotImplementedError("agent gates land in Plan 3")
    if isinstance(gate, StubGate):
        _emit(log, on_event, "gate_evaluated",
              {"kind": "stub", "action": _action_dict(gate.action)})
        return gate.action
    raise AssertionError(f"unknown gate type {type(gate).__name__}")


def _action_dict(action: Action) -> dict[str, Any]:
    if isinstance(action, Continue):
        return {"kind": "continue"}
    if isinstance(action, Retry):
        return {"kind": "retry"}
    if isinstance(action, Pause):
        return {"kind": "pause"}
    if isinstance(action, Goto):
        return {"kind": "goto", "target": action.target}
    return {"kind": "unknown"}


def _emit(log: EventLog, on_event: EventCallback, type_: str, payload: dict[str, Any]) -> None:
    log.append(type_, payload)
    on_event({"type": type_, "payload": payload})


def _emit_event(log: EventLog, on_event: EventCallback, event: dict[str, Any]) -> None:
    log.append(event["type"], event.get("payload", {}))
    on_event(event)
