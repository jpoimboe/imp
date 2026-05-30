"""Workflow executor (unified step model): a single flat cursor over the
workflow's steps. Each step is dispatched by kind (agent/script/human/branch),
its rules block is evaluated, and the resulting Action moves (or terminates)
the cursor.

The engine owns state.json + events.jsonl. The runner owns transcript.jsonl.
A context: fresh agent step wipes its transcript dir on entry; a context: resume
step keeps it and balances a trailing complete_step. The whole steps/ tree is
removed at change completion.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from enum import Enum
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
from imp.engine.feedback import (
    format_agent_feedback,
    format_script_feedback,
)
from imp.engine.gates import (
    apply_human_option,
    evaluate_rules,
    run_human,
    run_script,
)
from imp.engine.loader import load_workflow
from imp.engine.schema import validate_agent_output
from imp.engine.state import CurrentStep, EventLog, JsonlEventLog, State
from imp.engine.types import (
    Action,
    Continue,
    Goto,
    Pause,
    Retry,
    Step,
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

    flat = workflow.flat_steps()
    index_of = {s.name: i for i, s in enumerate(flat)}

    while True:
        if state.step is None or index_of.get(state.step) is None:
            return _complete(change, state, log)            # terminal
        idx = index_of[state.step]
        step = flat[idx]

        # var_mutations was ALREADY applied to state.vars and logged inside the
        # _route_* body — run_change receives it for visibility only and must NOT
        # re-apply it (double-apply trap). The third element is otherwise unused.
        action, feedback_text, _var_mutations = await _dispatch_step(
            step, workflow, config, change, state, log,
            human_input_fn=human_input_fn, human_output_fn=human_output_fn,
            user_chat_input_fn=user_chat_input_fn, coordinator=coordinator)

        if coordinator.should_pause or isinstance(action, Pause):
            state.status = "paused"
            state.save(change.state_dir)
            _emit(log, "workflow_paused", {})
            return RunResult.PAUSED
        if isinstance(action, Retry):
            raise AssertionError("Retry must not escape _dispatch_step")

        target_idx = None
        if isinstance(action, Continue):
            state.loop_counts.pop(step.name, None)          # forward advance resets
            nxt = idx + 1
            if nxt >= len(flat):
                return _complete(change, state, log)
            state.step = flat[nxt].name
        elif isinstance(action, Goto):
            target_idx = workflow.resolve_goto(action.target)
            if target_idx >= len(flat):                      # empty trailing phase
                return _complete(change, state, log)
            if target_idx <= idx:                            # backward OR self → loop-counted
                n = state.loop_counts.get(step.name, 0) + 1
                state.loop_counts[step.name] = n
                if n > step.max_loops:
                    state.status = "paused"
                    state.save(change.state_dir)
                    _emit(log, "workflow_paused",
                          {"reason": "max_loops", "step": step.name})
                    return RunResult.PAUSED
            else:
                state.loop_counts.pop(step.name, None)       # forward goto resets
            state.step = flat[target_idx].name
        else:
            raise AssertionError(f"unexpected action {action!r}")

        # stash optional wrapper feedback for the target step to consume
        if (isinstance(action, Goto) and feedback_text is not None
                and target_idx is not None and target_idx < len(flat)):
            state.retry_feedback = {
                "target_step": flat[target_idx].name, "text": feedback_text}
        state.save(change.state_dir)


def _complete(change: Change, state: State, log: EventLog) -> RunResult:
    state.status = "completed"
    state.step = None
    state.save(change.state_dir)
    shutil.rmtree(change.change_dir / "steps", ignore_errors=True)
    return RunResult.COMPLETED


async def _dispatch_step(
    step: Step, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, *,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
) -> tuple[Action, str | None, dict[str, object]]:
    """Returns (action, feedback_text, var_mutations)."""
    wrapper_text: str | None = None
    fb = state.retry_feedback
    if fb is not None and fb.get("target_step") == step.name:
        wrapper_text = fb.get("text")
        state.retry_feedback = None
        state.save(change.state_dir)

    if step.kind == "branch":
        return _route_branch(step, state, log)
    if step.kind == "script":
        return _route_script(step, change, state, log)
    if step.kind == "human":
        return await _route_human(step, state, log, human_input_fn, human_output_fn)
    return await _route_agent(
        step, workflow, config, change, state, log,
        user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
        wrapper_text=wrapper_text)


def _emit_started(log: EventLog, step: Step, **extra: Any) -> dict[str, Any]:
    return _emit(log, "step_started",
                 {"step": step.name, "kind": step.kind, "phase": step.phase, **extra},
                 agent_id=None)


def _finish(
    log: EventLog, step: Step, match: Any
) -> tuple[Action, str | None, dict[str, object]]:
    _emit(log, "step_completed", {
        "step": step.name, "kind": step.kind,
        "action": _action_payload(match.action),
        "var_mutations": match.var_mutations,
    })
    return match.action, None, match.var_mutations


def _route_branch(
    step: Step, state: State, log: EventLog
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step)
    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, match)            # branch goto carries NO feedback


def _route_script(
    step: Step, change: Change, state: State, log: EventLog
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step)
    out = run_script(step, cwd=change.workflow_dir,
                     env_extra=engine_env(project_root=change.project_root,
                                          artifacts_dir=change.artifacts_dir))
    match = evaluate_rules(step.rules, vars=state.vars, output=out)
    state.vars.update(match.var_mutations)
    action, _, muts = _finish(log, step, match)
    feedback = None
    if isinstance(action, Goto) and action.feedback:
        assert step.command is not None, "script step must have a command"
        feedback = format_script_feedback(
            step.command, out["exit"], out["stdout"], out["stderr"])
    return action, feedback, muts


async def _route_human(
    step: Step, state: State, log: EventLog,
    input_fn: HumanInputFn, output_fn: HumanOutputFn,
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step)          # step_started for ALL kinds — opens the phase node
    _emit(log, "human_prompt_pending",
          {"step": step.name, "phase": step.phase, "ask": step.ask,
           "options": [{"label": o.label} for o in step.options]})
    chosen = await run_human(step, input_fn=input_fn, output_fn=output_fn)
    muts = apply_human_option(chosen, vars=state.vars)
    state.vars.update(muts)
    _emit(log, "step_completed", {
        "step": step.name, "kind": "human",
        "action": _action_payload(chosen.action), "var_mutations": muts})
    return chosen.action, None, muts          # human goto carries NO feedback


async def _route_agent(
    step: Step, workflow: Workflow, config: Config, change: Change,
    state: State, log: EventLog, *,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, wrapper_text: str | None,
) -> tuple[Action, str | None, dict[str, object]]:
    # step.file is guaranteed non-None: loader requires file:/prompt:, and
    # _v_agent_has_file rejects inline prompt: agents this milestone.
    assert step.file is not None, "agent step must have a file this milestone"
    agent_def = workflow.agents[step.file.stem]
    transcript_dir = change.change_dir / "steps" / step.phase / step.name

    if step.context == "fresh":
        shutil.rmtree(transcript_dir, ignore_errors=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = False
        opening = wrapper_text or "begin"                  # fresh-seed via initial_messages
        retry_wrapper: str | None = None
    else:  # resume — keep the transcript, balance a trailing complete_step
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = True
        opening = "begin"
        retry_wrapper = wrapper_text

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)
    initial = (Message(role="user", content=(TextBlock(text=opening),)),)

    backoff = 0
    while True:
        agent_output, errored, paused, fatal, delay = await _run_agent_invocation(
            agent_id=f"step:{step.phase}/{step.name}", agent_def=agent_def,
            fs_permissions=step.fs_permissions, model_role=step.model_role,
            workflow=workflow, config=config, change=change, state=state, log=log,
            transcript_dir=transcript_dir,
            # MUST include "step" — _run_agent_invocation builds step_started as
            # {model, provider, tools, interactive, **extras}; the narrative tree
            # names/pairs the node on payload.step. Branch/script/human get it via
            # _emit_started; the agent path must match.
            step_started_payload_extras={
                "step": step.name, "kind": "agent", "phase": step.phase},
            interactive=step.interactive, user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator, prior_backoff_attempts=backoff,
            retry_feedback=retry_wrapper if backoff == 0 else None,
            balance_on_resume=balance,
            initial_messages=initial)
        if delay is None:
            break
        backoff += 1
        await anyio.sleep(delay)
        if coordinator.should_pause:
            return Pause(), None, {}

    if paused:
        return Pause(), None, {}
    if fatal:
        _emit(log, "step_completed",
              {"step": step.name, "kind": "agent",
               "action": _action_payload(Pause()), "var_mutations": {}})
        return Pause(), None, {}
    if errored or agent_output is None:
        state.step_attempt += 1
        if state.step_attempt > step.max_retries:
            state.step_attempt = 0
            _emit(log, "step_completed",
                  {"step": step.name, "kind": "agent",
                   "action": _action_payload(step.on_max_retries), "var_mutations": {}})
            return step.on_max_retries, None, {}     # Pause (validator forbids Retry here)
        return await _route_agent(step, workflow, config, change, state, log,
                                  user_chat_input_fn=user_chat_input_fn,
                                  coordinator=coordinator, wrapper_text=wrapper_text)

    state.step_attempt = 0
    match = evaluate_rules(step.rules, vars=state.vars, output=agent_output)
    state.vars.update(match.var_mutations)
    state.current_step = None
    state.save(change.state_dir)
    action, _, muts = _finish(log, step, match)
    feedback = (
        format_agent_feedback(step.name, agent_output)
        if isinstance(action, Goto) and action.feedback else None
    )
    return action, feedback, muts


def _action_payload(action: Action) -> dict[str, Any]:
    if isinstance(action, Continue):
        return {"verb": "continue"}
    if isinstance(action, Pause):
        return {"verb": "pause"}
    if isinstance(action, Goto):
        return {"verb": "goto", "target": action.target, "resume": action.resume}
    return {"verb": "unknown"}


_NUDGE_LIMIT = 2


def _build_continuation_fn(
    *,
    interactive: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    instruction: str,
    log: EventLog,
    agent_id: str | None,
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

        def _interactive(last_text: str) -> str | Awaitable[str]:
            # The agent ended its turn without complete_step → it is asking the
            # user. Announce the block (mirrors human_prompt_pending) BEFORE we
            # read input, so out-of-process drivers know input is awaited.
            # Provider-native stop_reason can't carry this — claude_code streams
            # per-block AssistantMessages and logs stop_reason=None on each.
            _emit(log, "awaiting_user_input", {"prompt": last_text},
                  agent_id=agent_id)
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
    initial_messages: tuple[Message, ...],
    prior_backoff_attempts: int = 0,
    retry_feedback: str | None = None,
    balance_on_resume: bool = False,
) -> tuple[dict[str, Any] | None, bool, bool, bool, float | None]:
    """Run an agent step to completion.

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
        log=log,
        agent_id=agent_id,
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
        initial_messages=initial_messages,
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
        retry_feedback=retry_feedback,
        balance_on_resume=balance_on_resume,
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


def _emit(log: EventLog, type_: str, payload: dict[str, Any], *,
          agent_id: str | None = None) -> dict[str, Any]:
    return log.append(agent_id=agent_id, type=type_, payload=payload)


def _emit_event(log: EventLog, event: dict[str, Any], *,
                agent_id: str | None = None) -> dict[str, Any]:
    return log.append(agent_id=agent_id, type=event["type"],
                      payload=event.get("payload", {}))
