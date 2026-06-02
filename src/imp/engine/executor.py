"""Workflow executor (unified step model): a single flat cursor over the
workflow's steps. Each step is dispatched by kind (agent/script/human/branch),
its rules block is evaluated, and the resulting Action moves (or terminates)
the cursor.

The engine owns state.json + events.jsonl. The runner owns transcript.jsonl.
An agent step entered fresh (no resume marker) wipes its transcript dir on
entry; a step entered via a `resume:` edge keeps it and balances a trailing
complete_step. The whole steps/ tree is removed at change completion.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import anyio

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import (
    ChangeContext,
    Message,
    TextBlock,
)
from imp.engine.agent_run import (
    AgentRunResult,
    build_agent_invocation,
    drive_invocation,
)
from imp.engine.change import Change
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
from imp.engine.model_tiers import effective_model_tiers
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
from imp.providers.config import load_config
from imp.providers.resolve import provider_from_model, resolve_roles

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

    prev_cwd = os.getcwd()
    os.chdir(change.control_root)  # spec §5.3: engine cwd = control root, so a
    # resume launched from inside a worktree never blocks `git worktree move`.
    # NOTE: os.chdir is process-global — this hard-assumes one run_change per
    # process (the foreground CLI model). Do not parallelize runs in-process
    # without revisiting (all git calls already pass explicit control_root).
    try:
        workflow = load_workflow(change.workflow_dir)
        config = load_config(change.control_root)
        log: EventLog = JsonlEventLog(change.events_path, on_event=on_event)
        state = State.load(change.state_dir)

        # Resolve roles once per run and persist, so resume never re-snapshots the
        # environment (design "When it runs"). run_change re-executes on every
        # resume, so guard on the persisted map.
        if state.resolved_roles is None:
            resolved, details = resolve_roles(workflow, config)
            state.resolved_roles = resolved
            state.model_tiers = effective_model_tiers(workflow, config)
            state.save(change.state_dir)
            _emit(log, "roles_resolved", {"roles": details}, agent_id=None)

        flat = workflow.flat_steps()
        index_of = {s.name: i for i, s in enumerate(flat)}

        while True:
            if state.step is None or index_of.get(state.step) is None:
                return _complete(change, state, log)            # terminal
            idx = index_of[state.step]
            step = flat[idx]
            is_resume = (state.resume_pending == state.step)

            # var_mutations was ALREADY applied to state.vars and logged inside the
            # _route_* body — run_change receives it for visibility only and must NOT
            # re-apply it (double-apply trap). The third element is otherwise unused.
            action, feedback_text, _var_mutations = await _dispatch_step(
                step, workflow, change, state, log, is_resume=is_resume,
                human_input_fn=human_input_fn, human_output_fn=human_output_fn,
                user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
                lint_command=config.lint_command)

            if coordinator.should_pause or isinstance(action, Pause):
                state.status = "paused"
                state.save(change.state_dir)
                # action may be Continue/Goto on a coordinator interrupt (no .reason).
                reason, kind = (
                    (action.reason, action.kind) if isinstance(action, Pause)
                    else ("interrupt", None)
                )
                payload: dict[str, Any] = {"reason": reason}
                if kind is not None:
                    payload["kind"] = kind
                _emit(log, "workflow_paused", payload)
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
                state.resume_pending = None
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
                if action.resume:
                    # Single-shot reset: a resumed step must not inherit a stale
                    # retry count. Done HERE (the navigation point that sets
                    # resume_pending), not in _route_agent — is_resume stays True
                    # across internal retries, so resetting per entry would let a
                    # persistent error retry forever.
                    state.step_attempt = 0
                    state.resume_pending = flat[target_idx].name
                else:
                    state.resume_pending = None
            else:
                raise AssertionError(f"unexpected action {action!r}")

            # stash optional wrapper feedback for the target step to consume
            if (isinstance(action, Goto) and feedback_text is not None
                    and target_idx is not None and target_idx < len(flat)):
                state.retry_feedback = {
                    "target_step": flat[target_idx].name, "text": feedback_text}
            state.save(change.state_dir)

            if state.pending_rename:
                log = apply_pending_rename(change, state, log, on_event=on_event)
    finally:
        # prev_cwd may have been removed (e.g. resume from inside a worktree this
        # run merged + tore down). Don't let the restore mask the result/exception.
        try:
            os.chdir(prev_cwd)
        except OSError:
            os.chdir(change.control_root)


def _complete(change: Change, state: State, log: EventLog) -> RunResult:
    """Terminal handler: marks the Change completed and, when ready_to_merge is
    set in state.vars, performs the full merge phase: merge imp/<slug> into
    base, journal the final artifacts, then remove the worktree (branch kept).

    Contract change from the original stub:
    - When ready_to_merge is NOT set: behaviour is identical to before — mark
      completed, drop steps/, return COMPLETED.
    - When ready_to_merge IS set: does git I/O (merge→journal→remove — three
      separate lock acquisitions) and CAN return PAUSED with
      state.status="merge_blocked" on worktree_dirty, CONFLICT, or BASE_DIRTY.
      Crash safety: merge→journal→remove are ordered so the worst-case mid-
      sequence crash leaves the merge commit already on base (durable) but the
      worktree still present (a harmless leftover); resume re-enters here, sees
      status=completed and worktree gone on success, or re-tries from state on
      block — never data loss, never a double-merge (resume rejects completed).
    """
    # M4: a rename requested on the very last step never reached the boundary
    # hook. Don't let it vanish silently — record why.
    if state.pending_rename:
        _emit(log, "rename_dropped", {"requested": state.pending_rename})
        state.pending_rename = None
    state.step = None  # terminal cursor (safe to persist early; status is NOT yet)

    # CRITICAL ordering: never write status="completed" until the run has truly
    # terminated successfully. A merge that blocks must leave a RESUMABLE status.
    if not state.vars.get("ready_to_merge"):
        # No merge gate: this is the terminal success — identical to old behaviour.
        state.status = "completed"
        state.save(change.state_dir)
        shutil.rmtree(change.change_dir / "steps", ignore_errors=True)
        return RunResult.COMPLETED

    from imp.engine import worktree

    # H1 safety net: merge_to_base merges the committed branch tip only. Any
    # uncommitted work would be SILENTLY LOST. Refuse — symmetric with
    # BASE_DIRTY — so the user commits and `imp resume`s to retry.
    if worktree.is_dirty(change.project_root):
        state.status = "merge_blocked"
        state.save(change.state_dir)
        _emit(log, "merge_blocked",
              {"reason": "worktree_dirty", "base": change.base_branch})
        return RunResult.PAUSED

    result = worktree.merge_to_base(change.control_root, change.slug, change.base_branch)
    if result.status in ("CONFLICT", "BASE_DIRTY"):
        state.status = "merge_blocked"
        state.save(change.state_dir)
        _emit(log, "merge_blocked",
              {"reason": result.status.lower(), "base": change.base_branch})
        return RunResult.PAUSED

    # MERGED — only NOW terminal success. Base ref already advanced (durable).
    # Mark completed, snapshot final state into the journal BEFORE removing the
    # worktree, then drop the worktree (branch kept).
    state.status = "completed"
    state.save(change.state_dir)
    _emit(log, "change_merged",
          {"base": change.base_branch, "commit": result.merge_commit})
    worktree.journal_commit(change.control_root, change.slug, {
        "events.jsonl": change.events_path,
        "change.json": change.change_dir / "change.json",
        "state.json": change.state_dir / "state.json",
    })
    worktree.remove_worktree(change.control_root, change.slug)  # steps/ goes with it
    return RunResult.COMPLETED


def apply_pending_rename(
    change: Change, state: State, log: EventLog, *, on_event: EventCallback | None,
) -> EventLog:
    """Apply state.pending_rename at a step boundary (spec §5.3). The worktree
    move is safe here: steps are sequential (no live tool subprocess), and the
    engine's cwd is the control root (set in run_change). Returns the event log
    to use going forward (re-opened at the possibly-new path)."""
    import json as _json

    from imp.engine import worktree
    from imp.engine.slug import dated_slug, slugify, split_date_prefix

    name = state.pending_rename
    assert name is not None
    old = change.slug
    # Preserve the YYYY-MM-DD- creation-date prefix: it is part of the Change's
    # identity and must not be dropped when the slug is re-derived from the new
    # name (spec §12). A date-less slug (legacy/test) falls back to plain slugify.
    date, _ = split_date_prefix(old)
    new = dated_slug(name, date=date) if date else slugify(name)
    if new == old:
        state.pending_rename = None
        state.save(change.state_dir)
        return log

    new_path = worktree.rename(change.control_root, old, new)
    if new_path is None:
        state.pending_rename = None
        state.save(change.state_dir)
        _emit(log, "rename_blocked", {"requested": new})
        return log

    # Repoint in-memory paths onto the moved worktree.
    change.slug = new
    change.title = name
    change.auto_titled = False   # renamed → the slug is now intentional (spec §12)
    change.project_root = new_path
    change.change_dir = new_path / ".imp" / "change"
    meta_path = change.change_dir / "change.json"
    meta = _json.loads(meta_path.read_text())          # preserves created_at
    meta["slug"], meta["title"] = new, name
    meta["auto_titled"] = False
    meta_path.write_text(_json.dumps(meta, indent=2, sort_keys=True))
    state.slug = new
    state.pending_rename = None
    state.save(change.state_dir)
    new_log = JsonlEventLog(change.events_path, on_event=on_event)
    _emit(new_log, "change_renamed", {"old_slug": old, "new_slug": new, "title": name})
    return new_log


async def _dispatch_step(
    step: Step, workflow: Workflow, change: Change,
    state: State, log: EventLog, *, is_resume: bool,
    human_input_fn: HumanInputFn, human_output_fn: HumanOutputFn,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, lint_command: str | None = None,
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
        return _route_script(step, change, state, log, lint_command=lint_command)
    if step.kind == "human":
        return await _route_human(step, state, log, human_input_fn, human_output_fn)
    return await _route_agent(
        step, workflow, change, state, log,
        user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
        wrapper_text=wrapper_text, is_resume=is_resume, lint_command=lint_command)


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
    step: Step, change: Change, state: State, log: EventLog,
    *, lint_command: str | None = None,
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step)
    out = run_script(step, cwd=change.workflow_dir,
                     env_extra=engine_env(project_root=change.project_root,
                                          change_slug=change.slug,
                                          change_title=change.title,
                                          lint_command=lint_command))
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
    step: Step, workflow: Workflow, change: Change,
    state: State, log: EventLog, *,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, wrapper_text: str | None, is_resume: bool,
    lint_command: str | None = None,
) -> tuple[Action, str | None, dict[str, object]]:
    # step.file is guaranteed non-None: loader requires file:/prompt:, and
    # _v_agent_has_file rejects inline prompt: agents this milestone.
    assert step.file is not None, "agent step must have a file this milestone"
    agent_def = workflow.agents[step.file.stem]
    transcript_dir = change.change_dir / "steps" / step.phase / step.name

    # Navigation-driven: keep the transcript only when this entry was reached
    # via a `resume:` edge (the resume_pending marker == this step).
    keep = is_resume
    if not keep:
        shutil.rmtree(transcript_dir, ignore_errors=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = False
        opening = wrapper_text or "begin"                  # fresh-seed via initial_messages
        retry_wrapper: str | None = None
    else:  # keep the transcript, balance a trailing complete_step
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = True
        opening = "begin"
        retry_wrapper = wrapper_text

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)
    initial = (Message(role="user", content=(TextBlock(text=opening),)),)

    backoff = 0
    while True:
        result = await _run_agent_invocation(
            agent_id=f"step:{step.phase}/{step.name}", agent_def=agent_def,
            fs_permissions=step.fs_permissions, model_role=step.model_role,
            workflow=workflow, change=change, state=state, log=log,
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
            initial_messages=initial, lint_command=lint_command)
        if result.delay is None:
            break
        backoff += 1
        # Self-healed-retry visibility (agent_retry): surface the backoff so a
        # transient network/rate_limit error that recovers doesn't look like a
        # failure. category comes from the plumbed ErrorInfo.
        _emit(log, "agent_retry", {
            "category": result.error.category if result.error else "unknown",
            "attempt": backoff,
            "backoff_seconds": result.delay,
        }, agent_id=None)
        await anyio.sleep(result.delay)
        if coordinator.should_pause:
            return Pause(), None, {}

    if result.paused:
        return Pause(), None, {}
    if result.fatal:
        # Defense in depth: reset the retry count when pausing fatally, matching
        # the success/exhaustion resets. Runs once (at the pause), unlike the
        # resume reset which is gated on the navigation point.
        state.step_attempt = 0
        pause = Pause(
            reason=result.error.category if result.error else None,
            kind=result.error.kind if result.error else None,
        )
        _emit(log, "step_completed",
              {"step": step.name, "kind": "agent",
               "action": _action_payload(pause), "var_mutations": {}})
        return pause, None, {}
    if result.errored or result.output is None:
        state.step_attempt += 1
        if state.step_attempt > step.max_retries:
            state.step_attempt = 0
            on_max = step.on_max_retries
            # on_max_retries is a configured action; when it is a Pause, stamp it
            # with THIS frame's classification (the just-failed attempt).
            if isinstance(on_max, Pause) and result.error is not None:
                on_max = Pause(reason=result.error.category, kind=result.error.kind)
            _emit(log, "step_completed",
                  {"step": step.name, "kind": "agent",
                   "action": _action_payload(on_max), "var_mutations": {}})
            return on_max, None, {}     # Pause (validator forbids Retry here)
        return await _route_agent(step, workflow, change, state, log,
                                  user_chat_input_fn=user_chat_input_fn,
                                  coordinator=coordinator, wrapper_text=wrapper_text,
                                  is_resume=is_resume, lint_command=lint_command)

    state.step_attempt = 0
    match = evaluate_rules(step.rules, vars=state.vars, output=result.output)
    state.vars.update(match.var_mutations)
    state.current_step = None
    state.save(change.state_dir)
    action, _, muts = _finish(log, step, match)
    feedback = (
        format_agent_feedback(step.name, result.output)
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


async def _run_agent_invocation(
    *,
    agent_id: str,
    agent_def: Any,
    fs_permissions: Any,
    model_role: str,
    workflow: Workflow,
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
    lint_command: str | None = None,
) -> AgentRunResult:
    """Run one top-level agent step to a terminal AgentRunResult.

    Computes the executor's caller-specific inputs (model resolution from the
    persisted role map, prompt rendering against the shared write-log,
    subagent_defs + max_tool_calls resolution), emits step_started, then defers
    construction and the drain loop to the shared builder + driver. The backoff
    sleep loop lives in _route_agent (it owns agent_retry events + transcript
    re-entry). See drive_invocation for the field semantics of the result.
    """
    assert state.resolved_roles is not None  # set by run_change before dispatch
    provider = provider_from_model(state.resolved_roles[model_role])

    rendered = render_prompt(
        agent_def.prompt_template_source, build_context(
            change_slug=change.slug, change_title=change.title,
            change_auto_titled=change.auto_titled, event_log=log,
            vars=state.vars,
        ),
    )

    subagent_defs = {
        name: workflow.agents[name]
        for name in agent_def.subagents
        if name in workflow.agents
    }

    # Spec ceiling resolution: agent.max_tool_calls -> workflow default -> None.
    if agent_def.max_tool_calls is not None:
        resolved_max_tool_calls = agent_def.max_tool_calls
    elif workflow.defaults.max_tool_calls is not None:
        resolved_max_tool_calls = workflow.defaults.max_tool_calls
    else:
        resolved_max_tool_calls = None

    # step_started is an executor lifecycle event (agent_id=None); its id seeds
    # the agent's first event (step_context) via first_event_parent_id.
    started = _emit(log, "step_started", {
        "model": provider.model,
        "provider": provider.name,
        "tools": sorted(agent_def.tools),
        "interactive": interactive,
        **step_started_payload_extras,
    }, agent_id=None)

    invocation = build_agent_invocation(
        agent_def=agent_def,
        provider_name=provider.name,
        model_name=provider.model,
        rendered_system_prompt=rendered,
        initial_messages=initial_messages,
        change_context=ChangeContext(
            slug=change.slug, title=change.title,
            project_root=change.project_root, workflow_dir=change.workflow_dir,
        ),
        interactive=interactive,
        user_chat_input_fn=user_chat_input_fn,
        transcript_dir=transcript_dir,
        fs_permissions=fs_permissions,
        events_path=change.events_path,
        subagent_defs=subagent_defs,
        model_tiers=state.model_tiers or (),
        max_tool_calls=resolved_max_tool_calls,
        agent_id=agent_id,
        event_log=log,
        change_state=state,
        first_event_parent_id=started["id"],
        resolved_roles=state.resolved_roles,
        coordinator=coordinator,
        subagent_depth=0,
        retry_feedback=retry_feedback,
        balance_on_resume=balance_on_resume,
        log=log,
        lint_command=lint_command,
    )

    runner = provider.create_runner(invocation)
    return await drive_invocation(
        invocation, runner, coordinator=coordinator, log=log,
        emit_events=True, prior_backoff_attempts=prior_backoff_attempts,
    )


def _emit(log: EventLog, type_: str, payload: dict[str, Any], *,
          agent_id: str | None = None) -> dict[str, Any]:
    return log.append(agent_id=agent_id, type=type_, payload=payload)
