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

import anyio

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.engine import error_policy
from imp.engine.change import Change
from imp.engine.completion import completion_instruction
from imp.engine.env import engine_env
from imp.engine.gates import (
    DecideNoMatch,
    evaluate_decide,
    run_human_gate,
    run_script_gate,
)
from imp.engine.loader import load_workflow, parse_agent_file
from imp.engine.schema import validate_agent_output
from imp.engine.state import CurrentStep, EventLog, JsonlEventLog, State
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
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,  # cli.signals.InterruptCoordinator | None
) -> RunResult:
    on_event = on_event or (lambda _e: None)
    if coordinator is None:
        # Lazy import to avoid circular dep at module-load time.
        from imp.cli.signals import InterruptCoordinator
        coordinator = InterruptCoordinator()

    workflow = load_workflow(change.workflow_dir)
    config = load_config(change.project_root)

    log = JsonlEventLog(change.events_path, on_event=on_event)
    state = State.load(change.state_dir)

    while True:
        if state.phase is None:
            state.status = "completed"
            state.save(change.state_dir)
            return RunResult.COMPLETED

        phase = next(p for p in workflow.phases if p.name == state.phase)
        action = await _execute_phase(
            phase, workflow, config, change, state, log,
            human_input_fn=human_input_fn, human_output_fn=human_output_fn,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
        )

        # Coordinator pause request takes precedence over the action's intent.
        if coordinator.should_pause:
            state.status = "paused"
            state.save(change.state_dir)
            _emit(log, "workflow_paused", {})
            return RunResult.PAUSED

        if isinstance(action, Pause):
            state.status = "paused"
            state.save(change.state_dir)
            _emit(log, "workflow_paused", {})
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
    state: State, log: EventLog,
    *,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
) -> Action:
    _emit(log, "phase_started", {"phase": phase.name})
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
                step, workflow, config, change, state, log,
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
                gate, workflow, config, change, state, log,
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
        _emit(log, "phase_completed", {"phase": phase.name})
        return Continue()


_NUDGE_LIMIT = 2


def _build_continuation_fn(
    *,
    interactive: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    instruction: str,
) -> Callable[[str], str | None | Awaitable[str | None]] | None:
    """Build the end-of-turn continuation hook the provider calls when an
    agent ends a turn without complete_step.

    interactive: wrap the user chat input — the human keeps the conversation
    going, so it always returns a string (an interactive agent ends only by
    calling complete_step). Preserves today's interactive-resume behavior.

    non-interactive: a fresh nudge counter per invocation. The first
    _NUDGE_LIMIT calls return the corrective instruction; after that, None —
    which makes the provider emit agent_incomplete (→ Pause).
    """
    if interactive:
        if user_chat_input_fn is None:
            return None

        def _interactive(_last_text: str) -> str | Awaitable[str]:
            return user_chat_input_fn("")

        return _interactive

    nudge = f"You ended your turn without calling `complete_step`. {instruction}"
    count = 0

    def _nudge(_last_text: str) -> str | None:
        nonlocal count
        if count >= _NUDGE_LIMIT:
            return None
        count += 1
        return nudge

    return _nudge


async def _run_agent_invocation(
    *,
    agent_id: str,
    agent_def: Any,
    fs_permissions: Any,
    model_role: str,
    workflow: Workflow,
    config: Config,
    change: Change,
    state: State,
    log: EventLog,
    transcript_dir: Any,
    step_started_payload_extras: dict[str, Any],
    interactive: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
    prior_backoff_attempts: int = 0,
) -> tuple[dict[str, Any] | None, bool, bool, bool, float | None]:
    """Run an agent (step OR gate) to completion.

    Returns ``(agent_output, errored, paused, fatal, backoff_delay)`` where:
    - ``agent_output`` is the validated output dict, or ``None`` if errored/paused.
    - ``errored`` is ``True`` when the run produced an error that warrants a retry.
    - ``paused`` is ``True`` when the coordinator requested a pause mid-run.
    - ``fatal`` is ``True`` when error_policy.decide returned Pause for the
      error's category; the caller turns this into ``Pause`` directly so the
      user sees the failure immediately and we don't burn LLM calls on retries
      that can't possibly succeed.
    - ``backoff_delay`` is a non-None float when error_policy.decide returned
      RetryWithBackoff for the error's category. The caller is expected to
      ``await anyio.sleep(backoff_delay)`` and re-invoke this function with
      an incremented ``prior_backoff_attempts``. None means "no backoff
      requested" (success, pause, or RetryNow).

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

    # Change 5: tell every agent — from its own output schema — to call
    # complete_step. Single source of truth shared with the nudge closure.
    instruction = completion_instruction(agent_def.output_schema)
    rendered = f"{rendered}\n\n{instruction}"

    continuation_fn = _build_continuation_fn(
        interactive=interactive,
        user_chat_input_fn=user_chat_input_fn,
        instruction=instruction,
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

    # step_started is an executor lifecycle event (not from the runner), so
    # agent_id=None — it does not anchor into the per-agent chain. Its id
    # seeds the agent's first event (step_context) via first_event_parent_id.
    started = _emit(log, "step_started", {
        "model": provider.model,
        "provider": provider.name,
        "tools": sorted(agent_def.tools),
        "interactive": interactive,
        **step_started_payload_extras,
    }, agent_id=None)

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
        continuation_fn=continuation_fn,
        agent_id=agent_id,
        event_log=log,
        first_event_parent_id=started["id"],  # chains step_context.parent_id to step_started
    )

    runner = provider.create_runner(invocation)
    agent_output: dict[str, Any] | None = None

    coordinator.attach_runner(runner.interrupt)
    try:
        async for event in runner.run(invocation):
            _emit_event(log, event, agent_id=invocation.agent_id)
            if event["type"] == "agent_output":
                agent_output = event["payload"]
            if event["type"] == "error":
                payload = event.get("payload") or {}
                category = payload.get("category", "unknown")
                retry_after = (payload.get("detail") or {}).get("retry_after_seconds")
                decision = error_policy.decide(
                    category,
                    prior_backoff_attempts=prior_backoff_attempts,
                    hint_delay_seconds=retry_after,
                )
                if isinstance(decision, error_policy.Pause):
                    return None, True, False, True, None
                if isinstance(decision, error_policy.RetryWithBackoff):
                    return None, True, False, False, decision.delay_seconds
                # RetryNow (agent_output_missing today)
                return None, True, False, False, None
            if coordinator.should_pause:
                # Coordinator triggered mid-step. Runner's interrupt() has fired.
                return None, False, True, False, None
    finally:
        coordinator.detach_runner()

    if agent_output is None:
        return None, True, False, False, None

    try:
        validate_agent_output(agent_output, agent_def.output_schema)
    except Exception as e:
        # Executor-synthesized error (not runner-yielded): agent_id=None.
        _emit(log, "error", {
            "category": "agent_output_missing",
            "kind": "output_validation",
            "message": f"output validation: {e}",
        }, agent_id=None)
        return None, True, False, False, None

    return agent_output, False, False, False, None


async def _execute_step(
    step: Step, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog,
    *,
    human_input_fn: HumanInputFn,
    human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
) -> Action:
    agent_def = workflow.agents[step.file.stem]
    assert state.phase is not None, "phase must be set before executing a step"
    transcript_dir = change.change_dir / "steps" / state.phase / step.name
    transcript_dir.mkdir(parents=True, exist_ok=True)

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)

    step_backoff_attempts = 0
    while True:
        agent_output, errored, paused, fatal, backoff_delay = await _run_agent_invocation(
            agent_id=f"step:{state.phase}/{step.name}",
            agent_def=agent_def,
            fs_permissions=step.fs_permissions,
            model_role=step.model_role,
            workflow=workflow, config=config, change=change,
            state=state, log=log,
            transcript_dir=transcript_dir,
            step_started_payload_extras={"step": step.name},
            interactive=step.interactive,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
            prior_backoff_attempts=step_backoff_attempts,
        )
        if backoff_delay is None:
            break
        step_backoff_attempts += 1
        await anyio.sleep(backoff_delay)
        if coordinator.should_pause:
            return Pause()

    if paused:
        return Pause()

    if fatal:
        _emit(log, "step_completed", {"step": step.name, "action": "pause"})
        return Pause()

    if errored or agent_output is None:
        _emit(log, "step_completed", {"step": step.name, "action": "retry"})
        return Retry()

    for gate in step.gates:
        action = await _run_gate(
            gate, workflow, config, change, state, log,
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
    _emit(log, "step_completed", {"step": step.name, "action": "continue"})
    return Continue()


def _next_gate_invocation_dir(gates_dir: Path, stem: str) -> Path:
    """Pick the next `<stem>-N/`. Monotonic: a wiped N is never reclaimed.

    Persists a per-stem high-water mark in `<gates_dir>/.hwm-<stem>` so the
    allocator returns max(highest_dir_seen, hwm) + 1. This guarantees one
    transcript_dir ⇒ one logical agent_id forever, even though successful
    gates wipe their dir and a step-re-run re-enters the allocator.
    """
    gates_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{stem}-"
    present = 0
    for entry in gates_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            suffix = entry.name[len(prefix):]
            if suffix.isdigit():
                present = max(present, int(suffix))
    hwm_file = gates_dir / f".hwm-{stem}"
    hwm = 0
    if hwm_file.exists():
        try:
            hwm = int(hwm_file.read_text().strip())
        except (ValueError, OSError):
            hwm = 0
    next_n = max(present, hwm) + 1
    hwm_file.write_text(str(next_n))
    return gates_dir / f"{stem}-{next_n}"


async def _run_agent_gate(
    gate: AgentGate, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog,
    *,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
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

    gate_backoff_attempts = 0
    while True:
        agent_output, errored, paused, fatal, backoff_delay = await _run_agent_invocation(
            agent_id=f"gate:{state.phase}/{transcript_dir.name}",
            agent_def=agent_def,
            fs_permissions=gate.fs_permissions,
            model_role=gate.model_role,
            workflow=workflow, config=config, change=change,
            state=state, log=log,
            transcript_dir=transcript_dir,
            step_started_payload_extras={"kind": "agent_gate", "gate_stem": stem},
            interactive=False,                            # gates are autonomous
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,                      # gate-internal bash etc.
                                                          # must honor Ctrl+C
            prior_backoff_attempts=gate_backoff_attempts,
        )
        if backoff_delay is None:
            break
        gate_backoff_attempts += 1
        await anyio.sleep(backoff_delay)
        if coordinator.should_pause:
            _emit(log, "gate_evaluated",
                  {"kind": "agent", "gate": stem, "action": _action_dict(Pause())})
            return Pause()

    if paused or fatal:
        _emit(log, "gate_evaluated",
              {"kind": "agent", "gate": stem, "action": _action_dict(Pause())})
        return Pause()

    if errored or agent_output is None:
        _emit(log, "gate_evaluated",
              {"kind": "agent", "gate": stem, "action": _action_dict(Retry())})
        return Retry()

    try:
        action = evaluate_decide(gate.decide, agent_output)
    except DecideNoMatch as e:
        # Validation (V13) now guarantees every decide has an `else` rule, so
        # this is a defensive, should-be-unreachable path. Surface to the human
        # rather than looping a phase retry that re-runs upstream steps.
        _emit(log, "error", {
            "category": "config",
            "kind": "decide_no_match",
            "message": f"agent gate decide: {e}",
        }, agent_id=None)
        _emit(log, "gate_evaluated",
              {"kind": "agent", "gate": stem, "action": _action_dict(Pause())})
        return Pause()

    # Clean up the gate's transcript dir on success (mirrors step success).
    shutil.rmtree(transcript_dir, ignore_errors=True)
    state.current_step = None
    state.save(change.state_dir)
    _emit(log, "gate_evaluated",
          {"kind": "agent", "gate": stem, "action": _action_dict(action)})
    return action


async def _run_gate(
    gate: Any, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog,
    *,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    agent_output_context: dict[str, Any] | None = None,
    coordinator: Any = None,
) -> Action:
    if isinstance(gate, HumanGate):
        _emit(log, "human_gate_pending", {
            "phase": state.phase,
            "prompt": gate.prompt,
            "options": [{"label": opt.label} for opt in gate.options],
        })
        action, _chosen = await run_human_gate(gate, input_fn=human_input_fn, output_fn=human_output_fn)
        _emit(log, "gate_evaluated", {"kind": "human", "action": _action_dict(action)})
        return action
    if isinstance(gate, ScriptGate):
        env_extra = engine_env(
            project_root=change.project_root, artifacts_dir=change.artifacts_dir,
        )
        action, ctx = run_script_gate(gate, cwd=change.workflow_dir, env_extra=env_extra)
        _emit(log, "gate_evaluated",
              {"kind": "script", "context": ctx, "action": _action_dict(action)})
        return action
    if isinstance(gate, AgentGate):
        return await _run_agent_gate(
            gate, workflow, config, change, state, log,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
        )
    if isinstance(gate, StubGate):
        _emit(log, "gate_evaluated",
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


def _emit(log: EventLog, type_: str, payload: dict[str, Any], *,
          agent_id: str | None = None) -> dict[str, Any]:
    return log.append(agent_id=agent_id, type=type_, payload=payload)


def _emit_event(log: EventLog, event: dict[str, Any], *,
                agent_id: str | None = None) -> dict[str, Any]:
    return log.append(agent_id=agent_id, type=event["type"],
                      payload=event.get("payload", {}))
