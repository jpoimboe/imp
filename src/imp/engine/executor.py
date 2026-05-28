"""Workflow executor (V1): async step dispatch, per-step transcript cleanup,
gate evaluation, state persistence.

The engine owns state.json + events.jsonl. The runner owns transcript.jsonl.
On successful step completion the engine deletes the step's transcript dir.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
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
    DecideNoMatch,
    evaluate_decide,
    run_human_gate,
    run_script_gate,
)
from imp.engine.loader import load_workflow, parse_agent_file
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
HumanInputFn = Callable[[], str | Awaitable[str]]
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
                human_output_fn=human_output_fn,
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
                coordinator=coordinator,
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


async def _run_agent_invocation(
    *,
    agent_def: Any,
    fs_permissions: Any,
    model_role: str,
    workflow: Workflow,
    config: Config,
    change: Change,
    state: State,
    log: EventLog,
    on_event: EventCallback,
    transcript_dir: Any,
    step_started_payload_extras: dict[str, Any],
    interactive: bool,
    user_chat_input_fn: Callable[[str], str] | None,
    coordinator: Any,
) -> tuple[dict[str, Any] | None, bool, bool, bool]:
    """Run an agent (step OR gate) to completion.

    Returns ``(agent_output, errored, paused, fatal)`` where:
    - ``agent_output`` is the validated output dict, or ``None`` if errored/paused.
    - ``errored`` is ``True`` when the run produced an error that warrants a retry.
    - ``paused`` is ``True`` when the coordinator requested a pause mid-run.
    - ``fatal`` is ``True`` when the error was a ``fatal_tool_error`` (or any other
      non-retryable misconfiguration); the caller turns this into ``Pause`` directly
      so the user sees the failure immediately and we don't burn LLM calls on retries
      that can't possibly succeed.

    The caller is responsible for:
    - setting/clearing ``state.current_step``
    - deciding what ``Action`` to return based on the output/errored/paused flags
    - cleaning up ``transcript_dir`` on success
    """
    role = config.roles.get(model_role) or config.roles["default"]
    provider = resolve_role(role)

    artifacts_dir = change.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)

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
        interactive=interactive,
        transcript_dir=transcript_dir,
        fs_permissions=fs_permissions,
        events_path=change.events_path,
        subagent_defs=subagent_defs,
        max_tool_calls=resolved_max_tool_calls,
        user_input_fn=user_chat_input_fn if interactive else None,
    )

    _emit(log, on_event, "step_started", {
        "model": invocation.model_name,
        "provider": provider.name,
        "tools": sorted(agent_def.tools),
        "interactive": interactive,
        **step_started_payload_extras,
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
                fatal = (event.get("payload") or {}).get("kind") == "fatal_tool_error"
                return None, True, False, fatal
            if coordinator.should_pause:
                # Coordinator triggered mid-step. Runner's interrupt() has fired.
                return None, False, True, False
    finally:
        coordinator.detach_runner()

    if agent_output is None:
        return None, True, False, False

    try:
        validate_agent_output(agent_output, agent_def.output_schema)
    except Exception as e:
        _emit(log, on_event, "error", {
            "category": "agent_output_missing",
            "message": f"output validation: {e}",
        })
        return None, True, False, False

    return agent_output, False, False, False


async def _execute_step(
    step: Step, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, on_event: EventCallback,
    *,
    human_input_fn: HumanInputFn,
    human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str] | None,
    coordinator: Any,
) -> Action:
    agent_def = workflow.agents[step.file.stem]
    assert state.phase is not None, "phase must be set before executing a step"
    transcript_dir = change.change_dir / "steps" / state.phase / step.name
    transcript_dir.mkdir(parents=True, exist_ok=True)

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)

    agent_output, errored, paused, fatal = await _run_agent_invocation(
        agent_def=agent_def,
        fs_permissions=step.fs_permissions,
        model_role=step.model_role,
        workflow=workflow, config=config, change=change,
        state=state, log=log, on_event=on_event,
        transcript_dir=transcript_dir,
        step_started_payload_extras={"step": step.name},
        interactive=step.interactive,
        user_chat_input_fn=user_chat_input_fn,
        coordinator=coordinator,
    )

    if paused:
        return Pause()

    if fatal:
        _emit(log, on_event, "step_completed", {"step": step.name, "action": "pause"})
        return Pause()

    if errored or agent_output is None:
        _emit(log, on_event, "step_completed", {"step": step.name, "action": "retry"})
        return Retry()

    for gate in step.gates:
        action = await _run_gate(
            gate, workflow, config, change, state, log, on_event,
            human_input_fn=human_input_fn,
            human_output_fn=human_output_fn,
            user_chat_input_fn=user_chat_input_fn,
            agent_output_context=agent_output,
            coordinator=coordinator,
        )
        if not isinstance(action, Continue):
            return action

    shutil.rmtree(transcript_dir, ignore_errors=True)
    state.current_step = None
    state.save(change.state_dir)
    _emit(log, on_event, "step_completed", {"step": step.name, "action": "continue"})
    return Continue()


def _next_gate_invocation_dir(gates_dir: Path, stem: str) -> Path:
    """Pick the next `<stem>-N/` under `gates_dir`. Stateless: scans existing
    dirs and returns max(N) + 1, or `<stem>-1/` if none exist. Survives
    resume cleanly (a paused gate's transcript dir survives in `gates_dir`
    until the engine deletes it on success, so the same N is picked up)."""
    if not gates_dir.is_dir():
        return gates_dir / f"{stem}-1"
    prefix = f"{stem}-"
    existing: list[int] = []
    for entry in gates_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix):]
        if suffix.isdigit():
            existing.append(int(suffix))
    next_n = (max(existing) + 1) if existing else 1
    return gates_dir / f"{stem}-{next_n}"


async def _run_agent_gate(
    gate: AgentGate, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, on_event: EventCallback,
    *,
    user_chat_input_fn: Callable[[str], str] | None,
    coordinator: Any,
) -> Action:
    """Execute an agent-as-gate. Same machinery as _execute_step minus the
    step-level gate loop. Decide block matches on the agent's structured output."""
    # Re-parse the gate's agent file each invocation. Cheap — agents files are
    # small. Avoids stale-cache surprises when a snapshot is edited.
    agent_def = parse_agent_file(gate.file)
    assert state.phase is not None

    stem = gate.file.stem
    gates_dir = change.change_dir / "steps" / state.phase / "gates"
    transcript_dir = _next_gate_invocation_dir(gates_dir, stem)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)

    agent_output, errored, paused, fatal = await _run_agent_invocation(
        agent_def=agent_def,
        fs_permissions=gate.fs_permissions,
        model_role=gate.model_role,
        workflow=workflow, config=config, change=change,
        state=state, log=log, on_event=on_event,
        transcript_dir=transcript_dir,
        step_started_payload_extras={"kind": "agent_gate", "gate_stem": stem},
        interactive=False,                            # gates are autonomous
        user_chat_input_fn=user_chat_input_fn,
        coordinator=coordinator,                      # gate-internal bash etc.
                                                      # must honor Ctrl+C
    )

    if paused or fatal:
        _emit(log, on_event, "gate_evaluated",
              {"kind": "agent", "gate": stem, "action": _action_dict(Pause())})
        return Pause()

    if errored or agent_output is None:
        _emit(log, on_event, "gate_evaluated",
              {"kind": "agent", "gate": stem, "action": _action_dict(Retry())})
        return Retry()

    try:
        action = evaluate_decide(gate.decide, agent_output)
    except DecideNoMatch as e:
        _emit(log, on_event, "error", {"message": f"agent gate decide: {e}"})
        return Retry()

    # Clean up the gate's transcript dir on success (mirrors step success).
    shutil.rmtree(transcript_dir, ignore_errors=True)
    state.current_step = None
    state.save(change.state_dir)
    _emit(log, on_event, "gate_evaluated",
          {"kind": "agent", "gate": stem, "action": _action_dict(action)})
    return action


async def _run_gate(
    gate: Any, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, on_event: EventCallback,
    *,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str] | None,
    agent_output_context: dict[str, Any] | None = None,
    coordinator: Any = None,
) -> Action:
    if isinstance(gate, HumanGate):
        _emit(log, on_event, "human_gate_pending", {
            "phase": state.phase,
            "prompt": gate.prompt,
            "options": [{"label": opt.label} for opt in gate.options],
        })
        action = await run_human_gate(gate, input_fn=human_input_fn, output_fn=human_output_fn)
        _emit(log, on_event, "gate_evaluated", {"kind": "human", "action": _action_dict(action)})
        return action
    if isinstance(gate, ScriptGate):
        env_extra = {"IMP_ARTIFACTS_DIR": str(change.artifacts_dir)}
        action, ctx = run_script_gate(gate, cwd=change.workflow_dir, env_extra=env_extra)
        _emit(log, on_event, "gate_evaluated",
              {"kind": "script", "context": ctx, "action": _action_dict(action)})
        return action
    if isinstance(gate, AgentGate):
        return await _run_agent_gate(
            gate, workflow, config, change, state, log, on_event,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
        )
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
