"""Workflow executor (unified step model): a single flat cursor over the
workflow's steps. Each step is dispatched by kind (agent/script/branch),
its rules block is evaluated, and the resulting Action moves (or terminates)
the cursor.

The engine owns state.json + events.jsonl. The runner owns transcript.jsonl.
An agent step entered fresh (no resume marker) wipes its transcript dir on
entry; a step entered via a `resume:` edge keeps it and balances a trailing
complete_step. The whole steps/ tree is removed at change completion.
"""

from __future__ import annotations

import functools
import os
import shutil
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
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
from imp.engine.backoff import (
    HEADLESS_RETRY_CEILING_SECONDS as _HEADLESS_RETRY_CEILING_SECONDS,
)
from imp.engine.backoff import (
    interruptible_sleep as _interruptible_sleep,
)
from imp.engine.change import Change
from imp.engine.env import engine_env, overlay_engine_env
from imp.engine.feedback import (
    format_script_feedback,
    render_output_fields,
)
from imp.engine.gates import (
    evaluate_rules,
    run_script,
)
from imp.engine.loader import load_workflow
from imp.engine.sandbox import (
    SandboxConfig,
    SandboxUnavailable,
    resolve_policy,
    sandbox_available,
)
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
from imp.providers.effective import load_effective_config
from imp.providers.resolve import provider_from_model, resolve_run

EventCallback = Callable[[dict[str, Any]], None]

OUTCOME_HISTORY_K = 4


def _record_outcome(state: State, step_name: str, output: dict[str, Any]) -> None:
    outcome = output.get("outcome")
    if not isinstance(outcome, str):
        return
    hist = state.outcome_history.setdefault(step_name, [])
    next_lap = hist[-1]["lap"] + 1 if hist else 1
    hist.append({"lap": next_lap, "outcome": outcome})
    del hist[:-OUTCOME_HISTORY_K]


def _render_prior_attempts(step_name: str, entries: list[dict[str, Any]]) -> str:
    lines = ["⟳ Prior attempts at this step:"]
    for e in entries:
        lines.append(f'   lap {e["lap"]}:  {step_name} — "{e["outcome"]}"')
    return "\n".join(lines)


class RunResult(Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    ERRORED = "errored"


def _latest_step_context_system_prompt(log: EventLog, agent_id: str) -> str | None:
    """The verbatim system_prompt from the LATEST step_context for this agent_id,
    or None if none exists. 'Latest' matters: a goto re-entry renders afresh and
    emits a newer step_context; resume must replay the newest, not an earlier row.
    Resume is not a hot path and the log is per-Change, so a linear scan is fine."""
    found: str | None = None
    for e in log.read():
        if e.get("type") == "step_context" and e.get("agent_id") == agent_id:
            found = (e.get("payload") or {}).get("system_prompt")
    return found


def _resume_system_prompt(
    log: EventLog, agent_id: str, transcript_dir: Any,
) -> str | None:
    """Resume-replay source. Returns the stored prompt to replay verbatim, or
    None when the caller should re-render. Aborts on the invariant violation:
    a non-empty transcript with no stored step_context means messages were
    rendered against a now-lost prompt — re-rendering would silently diverge."""
    from imp.agents.transcript import Transcript
    stored = _latest_step_context_system_prompt(log, agent_id)
    if stored is not None:
        return stored
    if Transcript(transcript_dir / "transcript.jsonl").exists_and_nonempty():
        raise RuntimeError(
            f"resume invariant violated: step {agent_id!r} has a non-empty "
            "transcript but no stored step_context system prompt; refusing to "
            "re-render against a lost prompt")
    return None       # empty transcript (pre-emit crash, or no-transcript provider)


def _resume_replays_transcript(transcript_dir: Any) -> bool:
    """True when a resumed step's kept transcript should be replayed. Every
    non-empty transcript is replayed and continued — a MISSING_COMPLETE_STEP
    transcript (autonomous turn ended without complete_step) is no longer wiped;
    the provider injects a synthetic `continue` user turn so it resumes as the
    ordinary SEND_TO_LLM case. Only an empty/absent transcript starts fresh: a
    `resume:` edge can target a step that never ran (e.g. a clean reintegration
    rebase skips the integrator, then integrate-verify fails and resumes it), and
    passively replaying nothing builds an empty query that hangs
    receive_response() forever."""
    from imp.agents.transcript import Transcript
    return Transcript(transcript_dir / "transcript.jsonl").exists_and_nonempty()


def _seed_messages(change: Change, wrapper_text: str | None) -> tuple[Message, ...]:
    """Fresh-entry seed. `<imp:begin/>` (or the wrapper text on a fresh goto),
    plus a name line when the Change is named — so a named Change conveys its
    handle through the immutable conversation record, never the cached prefix.
    Unnamed Changes get the begin token alone (no leaked provisional name). One
    TextBlock with a blank-line separator, so every provider's text join keeps
    the token and the name line apart on the wire."""
    opening = wrapper_text or "<imp:begin/>"
    text = (f"{opening}\n\nThis Change is named `{change.handle}`."
            if change.title.strip() else opening)
    return (Message(role="user", content=(TextBlock(text=text),)),)


def _liveness_on_event(coordinator: Any, on_event: EventCallback) -> EventCallback:
    """Compose subtree-liveness onto the run's `on_event`. Every append to the
    shared event log — by the task-lead OR any descendant subagent, since they
    all write through this one instance — pulses `coordinator.note_activity()`,
    which resets the B.2 inactivity watchdog (`agent_run._pull_with_inactivity_watchdog`).
    This is the signal that keeps flowing while a parent is parked in an in-process
    subagent dispatch and claude_code yields nothing; without it a busy subtree is
    falsely torn down every AGENT_INACTIVITY_TIMEOUT_S. The original driver
    callback is preserved (live narrative unaffected)."""
    def _composed(e: Any) -> None:
        coordinator.note_activity()
        on_event(e)
    return _composed


async def run_change(
    change: Change,
    *,
    on_event: EventCallback | None = None,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,  # cli.signals.InterruptCoordinator | None
    non_interactive: bool = False,
    seed_opening: str | None = None,
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None = None,
) -> RunResult:
    on_event = on_event or (lambda _e: None)
    if wait_for_turn_fn is None:
        wait_for_turn_fn = _headless_wait_for_turn
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
        snapshot = change.change_dir / "workflow"
        if not snapshot.is_dir():
            tmp = change.change_dir / "workflow.tmp"
            if tmp.exists():
                shutil.rmtree(tmp)
            shutil.copytree(change.live_workflow_dir, tmp)
            os.rename(tmp, snapshot)
        config = load_effective_config(
            project_root=change.project_root, control_root=change.control_root,
        ).config
        state = State.load(change.state_dir)
        # Freeze once per Change: resume reuses the facts/guidance the first run saw.
        if state.project_facts is None:
            state.project_facts = config.project_facts
            state.guidance = config.guidance
            state.save(change.state_dir)
        workflow = load_workflow(
            change.workflow_dir,
            project_keys=frozenset(state.project_facts),
        )
        log: EventLog = JsonlEventLog(
            change.events_path, on_event=_liveness_on_event(coordinator, on_event))
        sandbox_policy = _build_sandbox_policy(change, config, log)

        # Resolve tiers once per run and persist, so resume never re-snapshots the
        # environment (design "When it runs"). run_change re-executes on every
        # resume, so guard on the persisted map.
        if state.resolved_tiers is None:
            resolved_tiers, resolved_steps, details = resolve_run(workflow, config)
            state.resolved_tiers = resolved_tiers
            state.resolved_steps = resolved_steps
            state.save(change.state_dir)
            _emit(log, "tiers_resolved",
                  {"tiers": details["tiers"], "steps": details["steps"]},
                  agent_id=None)

        flat = workflow.flat_steps()
        index_of = {s.name: i for i, s in enumerate(flat)}
        pending_seed = seed_opening  # one-shot: consumed by the first fresh agent step

        while True:
            if state.completion_pending:
                # §3: re-entry after an interrupt parked at the completion
                # boundary. Finalize directly — no dispatch, no transcript wipe.
                state.completion_pending = False
                return _complete(change, state, log)
            if state.step is None:
                return _complete(change, state, log)            # genuine completion
            if index_of.get(state.step) is None:
                # The cursor names a step absent from the loaded workflow
                # (corruption, or a future --refresh-workflow re-snapshot). Never
                # silent-merge: pause with an error instead of calling _complete.
                state.status = "paused"
                state.save(change.state_dir)
                _emit_paused_event(log, "unknown_step", step=state.step)
                return RunResult.PAUSED
            idx = index_of[state.step]
            step = flat[idx]
            is_resume = (state.resume_pending == state.step)

            seed_for_step = (
                pending_seed
                if pending_seed is not None
                and step.kind == "agent" and not is_resume
                else None)
            # var_mutations was ALREADY applied to state.vars and logged inside the
            # _route_* body — run_change receives it for visibility only and must NOT
            # re-apply it (double-apply trap). The third element is otherwise unused.
            action, feedback_text, _var_mutations = await _dispatch_step(
                step, workflow, change, state, log, is_resume=is_resume,
                user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
                verify_command=config.verify_command,
                verify_timeout=config.verify_timeout,
                non_interactive=non_interactive,
                sandbox_policy=sandbox_policy,
                seed_opening=seed_for_step,
                project_facts=state.project_facts or {},
                guidance=state.guidance or "",
                wait_for_turn_fn=wait_for_turn_fn)
            if seed_for_step is not None:
                pending_seed = None

            if isinstance(action, Retry):
                raise AssertionError("Retry must not escape _dispatch_step")

            # §3 case 1 — mid-flight Pause: pause BEFORE navigation (as today).
            if isinstance(action, Pause):
                if step.kind == "agent":
                    # Any agent-step pause (quota, agent_incomplete, a fatal
                    # provider error, OR a user interrupt) keeps the current
                    # step's transcript so resume replays and continues it, not
                    # re-runs fresh. The replay decision is driven by whether the
                    # step has a kept transcript, not by how the run stopped.
                    state.resume_pending = state.step
                reason = action.reason or "interrupt"
                state.status = (
                    "merge_blocked" if reason == "merge_blocked" else "paused")
                state.save(change.state_dir)
                _emit_paused_event(
                    log, reason, kind=action.kind,
                    interrupt_reason=(coordinator.pause_reason
                                      if coordinator.should_pause else None))
                return RunResult.PAUSED

            target_idx = None
            if isinstance(action, Continue):
                state.loop_counts.pop(step.name, None)          # forward advance resets
                nxt = idx + 1
                if nxt >= len(flat):
                    return _complete_or_park(change, state, log, coordinator)
                state.step = flat[nxt].name
                state.resume_pending = None
            elif isinstance(action, Goto):
                target_idx = workflow.resolve_goto(action.target)
                if target_idx >= len(flat):                      # empty trailing phase
                    return _complete_or_park(change, state, log, coordinator)
                if target_idx <= idx:                            # backward OR self → loop-counted
                    n = state.loop_counts.get(step.name, 0) + 1
                    state.loop_counts[step.name] = n
                    if n > step.max_loops and not coordinator.should_pause:
                        state.status = "paused"
                        state.save(change.state_dir)
                        _emit_paused_event(log, "max_loops", step=step.name)
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

            # Stash wrapper feedback for the target step to consume via the
            # fresh-entry opening message: the goto's rework feedback and/or a
            # prior-attempts memory of the target step's own recent outcomes.
            if isinstance(action, Goto) and target_idx is not None and target_idx < len(flat):
                target_name = flat[target_idx].name
                preamble = None
                if target_idx <= idx:  # backward or self goto = a rework re-entry
                    entries = state.outcome_history.get(target_name)
                    if entries:
                        preamble = _render_prior_attempts(target_name, entries)
                parts = [p for p in (preamble, feedback_text) if p is not None]
                if parts:
                    state.retry_feedback = {
                        "target_step": target_name, "text": "\n\n".join(parts)}

            # §3 case 2 — an in-flight interrupt pauses AFTER navigation, so
            # re-entry starts the navigated-to step and §2 pending texts reach it.
            # Navigation already set resume_pending correctly (Goto(resume=True)
            # kept the target; Continue/forward-Goto cleared it) — do NOT override.
            if coordinator.should_pause:
                state.status = "paused"
                state.save(change.state_dir)
                _emit_paused_event(
                    log, "interrupt", interrupt_reason=coordinator.pause_reason)
                return RunResult.PAUSED
            state.save(change.state_dir)
    finally:
        # prev_cwd may have been removed (e.g. resume from inside a worktree this
        # run merged + tore down). Don't let the restore mask the result/exception.
        try:
            os.chdir(prev_cwd)
        except OSError:
            os.chdir(change.control_root)


def _complete_or_park(
    change: Change, state: State, log: EventLog, coordinator: Any,
) -> RunResult:
    """Navigation ran off the end. Normally finalize via _complete(); but an
    in-flight interrupt parks at completion instead (§3): set completion_pending,
    leave state.step on the last completed step's name unchanged (setting it to
    None would auto-complete on re-entry; completion_pending distinguishes the
    two), persist paused, emit. Re-entry — only on a /continue or Ctrl-D resume —
    runs the real _complete with no dispatch and no transcript wipe."""
    if coordinator.should_pause:
        state.completion_pending = True
        state.status = "paused"
        state.save(change.state_dir)
        _emit_paused_event(log, "interrupt", interrupt_reason=coordinator.pause_reason)
        return RunResult.PAUSED
    return _complete(change, state, log)


def _complete(change: Change, state: State, log: EventLog) -> RunResult:
    """Terminal handler. Two paths:
    - No merge happened (merge-to-base was never reached, or the gate never set
      it): mark completed, drop steps/, COMPLETED — unchanged legacy behaviour.
    - merge-to-base already merged under the lock (state.merge_commit set, base ref
      durably advanced): finalize — mark completed, journal the final artifacts,
      then remove the worktree (branch kept). Ordering is crash-safe: status is
      written + saved BEFORE journal/remove, so a crash mid-finalize leaves the
      merge durable and the worktree a harmless leftover. Recovery: a crash AFTER
      status='completed' is already done; a crash in the window between _route_merge
      recording merge_commit and this status write leaves status='running' — re-running
      re-enters _route_merge, which short-circuits on state.merge_commit (idempotent,
      no second merge) and completes cleanly.

    _complete no longer does `git merge` — the authoritative merge moved into the
    routable merge-to-base step (B3/F1), and the H1 dirty net moved into
    merge_to_base under the lock (R5)."""
    state.step = None
    if not state.merge_commit:
        state.status = "completed"
        state.save(change.state_dir)
        shutil.rmtree(change.change_dir / "steps", ignore_errors=True)
        return RunResult.COMPLETED

    from imp.engine import worktree
    state.status = "completed"
    state.save(change.state_dir)
    _emit(log, "change_merged",
          {"base": change.base_branch, "commit": state.merge_commit})
    from imp.engine import merge_queue
    merge_queue.pop_and_wake(change.control_root, change.id, change.base_branch)
    worktree.fold_wip_into_journal(change.control_root, change.id, {
        "events.jsonl": change.events_path,
        "change.json": change.change_dir / "change.json",
        "state.json": change.state_dir / "state.json",
    })
    worktree.remove_worktree(change.control_root, change.id)  # steps/ goes with it
    return RunResult.COMPLETED


async def _dispatch_step(
    step: Step, workflow: Workflow, change: Change,
    state: State, log: EventLog, *, is_resume: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, verify_command: str | None = None,
    verify_timeout: int | None = None,
    non_interactive: bool = False,
    sandbox_policy: Any = None,
    seed_opening: str | None = None,
    project_facts: dict[str, str] | None = None,
    guidance: str = "",
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None = None,
) -> tuple[Action, str | None, dict[str, object]]:
    """Returns (action, feedback_text, var_mutations)."""
    wrapper_text: str | None = None
    fb = state.retry_feedback
    if fb is not None and fb.get("target_step") == step.name:
        wrapper_text = fb.get("text")
        state.retry_feedback = None
        state.save(change.state_dir)

    if step.kind == "wait_for_head":
        if wait_for_turn_fn is None:
            raise RuntimeError("wait_for_turn_fn is required for wait_for_head steps")
        return await _route_wait_for_head(step, change, state, log, wait_for_turn_fn)
    if step.kind == "dequeue_wake":
        return _route_dequeue_wake(step, change, state, log)
    if step.kind == "branch":
        return _route_branch(step, state, log)
    if step.kind == "merge":
        return await _route_merge(step, change, state, log)
    if step.kind == "script":
        return await _route_script(step, change, state, log,
                                   verify_command=verify_command,
                                   verify_timeout=verify_timeout,
                                   sandbox_policy=sandbox_policy)
    return await _route_agent(
        step, workflow, change, state, log,
        user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
        wrapper_text=wrapper_text, is_resume=is_resume, verify_command=verify_command,
        non_interactive=non_interactive,
        sandbox_policy=sandbox_policy, seed_opening=seed_opening,
        project_facts=project_facts, guidance=guidance)


def _step_started_payload(step: Step) -> dict[str, Any]:
    return {"step": step.name, "kind": step.kind, "phase": step.phase,
            "quiet": step.quiet}


def _emit_started(log: EventLog, step: Step, **extra: Any) -> dict[str, Any]:
    return _emit(log, "step_started",
                 {**_step_started_payload(step), **extra},
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


async def _route_script(
    step: Step, change: Change, state: State, log: EventLog,
    *, verify_command: str | None = None, verify_timeout: int | None = None,
    sandbox_policy: Any = None,
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step)
    # The script gate's subprocess runs to completion under a blocking
    # proc.communicate() (gates.run_script). Offload it to a worker thread so the
    # event loop keeps turning — otherwise a multi-minute verify gate starves
    # every concurrent task on the loop (the live-status tick() animator, the
    # signal receiver, the remote-channel poller), freezing the UI at
    # "running · 0s" until the command returns. Only the blocking wait moves off
    # the loop; the event emits stay on the loop thread (single terminal writer).
    out = await anyio.to_thread.run_sync(
        functools.partial(
            run_script, step, cwd=change.workflow_dir,
            env_extra=engine_env(project_root=change.project_root,
                                 change_id=change.id,
                                 change_handle=change.handle,
                                 change_title=change.title,
                                 verify_command=verify_command,
                                 verify_timeout=verify_timeout),
            policy=sandbox_policy))
    match = evaluate_rules(step.rules, vars=state.vars, output=out)
    state.vars.update(match.var_mutations)
    action, _, muts = _finish(log, step, match)
    feedback = None
    if isinstance(action, Goto):
        assert step.command is not None, "script step must have a command"
        # exit-0 Goto is a success forward-route; only non-zero exit is a failure-retry.
        if out["exit"] != 0:
            feedback = format_script_feedback(
                step.command, out["exit"], out["stdout"], out["stderr"])
    elif isinstance(action, Pause):
        # Script pauses (e.g. integrate-rebase exit 3/4/5,
        # autosquash/stamp-trailers) otherwise lose their stderr — the pause verb
        # carries no reason and the CLI keys remediation only on merge_blocked.
        # Journal it so `imp log` shows it and the CLI can surface it (findings #5/#6).
        _emit(log, "script_blocked", {
            "step": step.name, "exit": out["exit"],
            "stdout": out["stdout"], "stderr": out["stderr"]})
    return action, feedback, muts


def _decisions_body(spec_path: Path) -> str | None:
    if not spec_path.is_file():
        return None
    lines = spec_path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = "decision" in line.lower()
            continue
        if capturing:
            out.append(line)
    text = "\n".join(out).strip()
    return text or None


async def _headless_wait_for_turn(predicate: Callable[[], bool]) -> None:
    """Default wait_for_turn used when run_change runs without a session server
    (tests, or a non-daemon caller): proceed iff already head, else hibernate."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    if predicate():
        return
    raise AwaitingUserInput()


async def _route_wait_for_head(
    step: Step, change: Change, state: State, log: EventLog,
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]],
) -> tuple[Action, str | None, dict[str, object]]:
    """Enqueue-if-absent, then proceed when head; otherwise wait. A non-head
    change with a client attached stays resident (wait_for_turn_fn polls the
    queue and watches detach); with no client it hibernates (AwaitingUserInput ->
    a clean paused exit, rehydrated later by the popping head's `imp resume`)."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    from imp.engine import merge_queue
    _emit_started(log, step)
    merge_queue.enqueue_if_absent(change.control_root, change.id, change.base_branch)

    def _is_head() -> bool:
        return merge_queue.is_head(change.control_root, change.id, change.base_branch)

    if not _is_head():
        ahead = merge_queue.position(change.control_root, change.id, change.base_branch)
        _emit(log, "waiting_for_head", {"base": change.base_branch, "ahead": ahead})
        try:
            await wait_for_turn_fn(_is_head)
        except AwaitingUserInput:
            pause = Pause(reason="awaiting_turn")
            _emit(log, "step_completed", {
                "step": step.name, "kind": step.kind,
                "action": _action_payload(pause), "var_mutations": {}})
            return pause, None, {}

    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, match)


def _route_dequeue_wake(
    step: Step, change: Change, state: State, log: EventLog,
) -> tuple[Action, str | None, dict[str, object]]:
    from imp.engine import merge_queue
    _emit_started(log, step)
    merge_queue.pop_and_wake(change.control_root, change.id, change.base_branch)
    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, match)


async def _route_merge(
    step: Step, change: Change, state: State, log: EventLog,
) -> tuple[Action, str | None, dict[str, object]]:
    """Engine-native authoritative merge (B3/F1). Holds RepoGitLock (inside
    merge_to_base), re-asserts a clean worktree (DIRTY) under the lock, performs
    the merge, and routes on the result via the step's rules. On MERGED it stashes
    the commit in State.merge_commit; the actual finalize (journal + worktree
    removal + mark completed) happens when the run falls through to _complete."""
    from imp.engine import worktree
    _emit_started(log, step)

    # Idempotent resume (crash-safety): if a prior run already merged (the commit
    # is durable on base) but crashed before _complete finalized, do NOT re-merge.
    if state.merge_commit:
        match = evaluate_rules(step.rules, vars=state.vars, output={"merge": "MERGED"})
        state.vars.update(match.var_mutations)
        return _finish(log, step, match)

    # merge_to_base holds RepoGitLock and shells out to git (merge/commit); a
    # large or conflict-heavy merge can take seconds-to-minutes. Offload it to a
    # worker thread so the event loop keeps turning — same rationale as the
    # script gate (see _route_script). The surrounding emits and state.save stay
    # on the loop thread (single terminal writer / no cross-thread state race).
    spec_path = change.change_dir / "artifacts" / "spec" / f"{change.handle}.md"
    body = _decisions_body(spec_path)
    result = await anyio.to_thread.run_sync(
        functools.partial(
            worktree.merge_to_base,
            change.control_root, change.id, change.base_branch, title=change.title,
            body=body, project_root=change.project_root))
    if result.status == "MERGED":
        state.merge_commit = result.merge_commit
        state.save(change.state_dir)

    # Preserve the pre-B3 merge_blocked surfacing (review finding #2). Before the
    # cutover, _complete emitted a `merge_blocked` event and set
    # status="merge_blocked"; the CLI keys remediation, resumability, and status
    # color on BOTH. The declared `{else: pause: true}` rule would produce a
    # reason-less pause -> status="paused" -> all of that goes dead. So for
    # blocking outcomes, emit the event ourselves and return a *reasoned* Pause;
    # run_change's pause handler maps reason=="merge_blocked" to the merge_blocked
    # status (Step 4b). DIRTY reuses the worktree_dirty reason the old H1 net emitted.
    if result.status in ("CONFLICT", "REFUSED", "DIRTY"):
        reason = "worktree_dirty" if result.status == "DIRTY" else result.status.lower()
        _emit(log, "merge_blocked",
              {"reason": reason, "base": change.base_branch, "detail": result.detail})
        pause = Pause(reason="merge_blocked", kind=reason)
        _emit(log, "step_completed",
              {"step": step.name, "kind": "merge",
               "action": _action_payload(pause), "var_mutations": {}})
        return pause, None, {}

    # MERGED -> continue (falls through to finalize).
    match = evaluate_rules(step.rules, vars=state.vars, output={"merge": result.status})
    state.vars.update(match.var_mutations)
    return _finish(log, step, match)


async def _route_agent(
    step: Step, workflow: Workflow, change: Change,
    state: State, log: EventLog, *,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, wrapper_text: str | None, is_resume: bool,
    verify_command: str | None = None,
    non_interactive: bool = False,  # §10/§12: consumed by the headless wall-clock retry ceiling
    sandbox_policy: Any = None,
    seed_opening: str | None = None,
    project_facts: dict[str, str] | None = None,
    guidance: str = "",
) -> tuple[Action, str | None, dict[str, object]]:
    # step.file is guaranteed non-None: loader requires file:/prompt:, and
    # _v_agent_has_file rejects inline prompt: agents this milestone.
    assert step.file is not None, "agent step must have a file this milestone"
    agent_def = workflow.agents[step.file.stem]
    transcript_dir = change.change_dir / "steps" / step.phase / step.name

    # Keep (replay) the transcript on every `resume:` entry that has a non-empty
    # one. A transcript that ended before complete_step is replayed and
    # continued (the provider injects a `continue` nudge), not re-run fresh; only
    # an empty/absent transcript starts fresh.
    keep = is_resume and _resume_replays_transcript(transcript_dir)
    if not keep:
        shutil.rmtree(transcript_dir, ignore_errors=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = False
        retry_wrapper: str | None = None
        opening = seed_opening if seed_opening is not None else wrapper_text
        initial: tuple[Message, ...] = _seed_messages(change, opening)
    else:  # keep the transcript, balance a trailing complete_step
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = True
        # No opener on resume: providers replay the on-disk transcript and
        # never iterate initial_messages. retry_wrapper rides on
        # retry_feedback (balance_success_on_resume), a separate channel.
        retry_wrapper = wrapper_text
        initial = ()

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)

    backoff = 0
    cumulative_backoff = 0.0
    while True:
        result = await _run_agent_invocation(
            agent_id=f"step:{step.phase}/{step.name}", agent_def=agent_def,
            fs_permissions=step.fs_permissions, tier=step.tier, step_name=step.name,
            workflow=workflow, change=change, state=state, log=log,
            transcript_dir=transcript_dir,
            step_started_payload_extras=_step_started_payload(step),
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator, prior_backoff_attempts=backoff,
            retry_feedback=retry_wrapper if backoff == 0 else None,
            balance_on_resume=balance,
            initial_messages=initial, is_resume=is_resume,
            verify_command=verify_command, non_interactive=non_interactive,
            sandbox_policy=sandbox_policy,
            project_facts=project_facts, guidance=guidance)
        if result.delay is None:
            break
        # §10/§12 headless backstop: an unattended run has no human to Ctrl-C, so
        # cap cumulative transient-error backoff by wall clock and pause instead of
        # hanging. Checked BEFORE the agent_retry emit so the journal shows a clean
        # ceiling pause, not a retry-then-pause. Interactive runs stay unbounded.
        if non_interactive and cumulative_backoff + result.delay > _HEADLESS_RETRY_CEILING_SECONDS:
            pause = Pause(reason="retry_ceiling",
                          kind=result.error.kind if result.error else None)
            _emit(log, "step_completed",
                  {"step": step.name, "kind": "agent",
                   "action": _action_payload(pause), "var_mutations": {}})
            return pause, None, {}
        backoff += 1
        # Self-healed-retry visibility (agent_retry): surface the backoff so a
        # transient network/rate_limit error that recovers doesn't look like a
        # failure. category comes from the plumbed ErrorInfo.
        _emit(log, "agent_retry", {
            "category": result.error.category if result.error else "unknown",
            "attempt": backoff,
            "backoff_seconds": result.delay,
        }, agent_id=None)
        await _interruptible_sleep(result.delay, coordinator)
        cumulative_backoff += result.delay
        if coordinator.should_pause:
            return Pause(), None, {}

    if result.paused:
        if result.awaiting_user:
            # Headless wait_for_user: mark THIS step resumable so the reload
            # keeps the transcript (the dangling wait_for_user tool_use is what
            # the AWAITING_USER resume resolves). Mirrors the resume: edge's
            # marker; cleared on the next forward Continue.
            state.resume_pending = state.step
            state.save(change.state_dir)
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
                                  is_resume=is_resume, verify_command=verify_command,
                                  non_interactive=non_interactive,
                                  sandbox_policy=sandbox_policy,
                                  seed_opening=seed_opening,
                                  project_facts=project_facts, guidance=guidance)

    state.step_attempt = 0
    _record_outcome(state, step.name, result.output)
    match = evaluate_rules(step.rules, vars=state.vars, output=result.output)
    state.vars.update(match.var_mutations)
    state.current_step = None
    state.save(change.state_dir)
    action, _, muts = _finish(log, step, match)
    feedback = None
    if isinstance(action, Goto) and action.feedback is not None:
        feedback = f"{action.feedback}\n{render_output_fields(result.output)}"
    return action, feedback, muts


def _action_payload(action: Action) -> dict[str, Any]:
    if isinstance(action, Continue):
        return {"verb": "continue"}
    if isinstance(action, Pause):
        return {"verb": "pause"}
    if isinstance(action, Goto):
        return {"verb": "goto", "target": action.target,
                "resume": action.resume, "rework": action.feedback is not None}
    return {"verb": "unknown"}


async def _run_agent_invocation(
    *,
    agent_id: str,
    agent_def: Any,
    fs_permissions: Any,
    tier: str,
    step_name: str,
    workflow: Workflow,
    change: Change,
    state: State,
    log: EventLog,
    transcript_dir: Any,
    step_started_payload_extras: dict[str, Any],
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
    initial_messages: tuple[Message, ...],
    is_resume: bool = False,
    prior_backoff_attempts: int = 0,
    retry_feedback: str | None = None,
    balance_on_resume: bool = False,
    verify_command: str | None = None,
    non_interactive: bool = False,
    sandbox_policy: Any = None,
    project_facts: dict[str, str] | None = None,
    guidance: str = "",
) -> AgentRunResult:
    """Run one top-level agent step to a terminal AgentRunResult.

    Computes the executor's caller-specific inputs (model resolution from the
    persisted tier map, prompt rendering against the shared write-log,
    subagent_defs + max_tool_calls resolution), emits step_started, then defers
    construction and the drain loop to the shared builder + driver. The backoff
    sleep loop lives in _route_agent (it owns agent_retry events + transcript
    re-entry). See drive_invocation for the field semantics of the result.
    """
    assert state.resolved_tiers is not None  # set by run_change before dispatch
    # Per-step override if differ_from was used, else the tier default
    resolved_steps = state.resolved_steps or {}
    if step_name in resolved_steps:
        provider_model = resolved_steps[step_name]
    else:
        provider_model = state.resolved_tiers[tier]
    provider = provider_from_model(provider_model)

    stored = _resume_system_prompt(log, agent_id, transcript_dir) if is_resume else None
    if stored is not None:
        rendered = stored
        system_prompt_override: str | None = stored
    else:
        rendered = render_prompt(
            agent_def.prompt_template_source, build_context(
                change_id=change.id, change_handle=change.handle, change_title=change.title,
                event_log=log, vars=state.vars,
                project_facts=project_facts, guidance=guidance,
            ),
        )
        system_prompt_override = None

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
        **step_started_payload_extras,
    }, agent_id=None)

    invocation = build_agent_invocation(
        agent_def=agent_def,
        provider_name=provider.name,
        model_name=provider.model,
        rendered_system_prompt=rendered,
        system_prompt_override=system_prompt_override,
        initial_messages=initial_messages,
        change_context=ChangeContext(
            id=change.id, handle=change.handle, title=change.title,
            project_root=change.project_root, workflow_dir=change.workflow_dir,
            project_facts=project_facts or {}, guidance=guidance,
        ),
        user_chat_input_fn=user_chat_input_fn,
        transcript_dir=transcript_dir,
        fs_permissions=fs_permissions,
        events_path=change.events_path,
        subagent_defs=subagent_defs,
        tiers=tuple(state.resolved_tiers.keys()) if state.resolved_tiers else (),
        max_tool_calls=resolved_max_tool_calls,
        agent_id=agent_id,
        event_log=log,
        change_state=state,
        change=change,
        first_event_parent_id=started["id"],
        resolved_tiers=state.resolved_tiers,
        coordinator=coordinator,
        subagent_depth=0,
        retry_feedback=retry_feedback,
        balance_on_resume=balance_on_resume,
        log=log,
        verify_command=verify_command,
        non_interactive=non_interactive,
        sandbox_policy=sandbox_policy,
        inject_outcome_field=True,
    )

    runner = provider.create_runner(invocation)
    return await drive_invocation(
        invocation, runner, coordinator=coordinator, log=log,
        emit_events=True, prior_backoff_attempts=prior_backoff_attempts,
    )


def _emit(log: EventLog, type_: str, payload: dict[str, Any], *,
          agent_id: str | None = None) -> dict[str, Any]:
    return log.append(agent_id=agent_id, type=type_, payload=payload)


def _emit_paused_event(
    log: EventLog, reason: str, *, kind: str | None = None,
    step: str | None = None, interrupt_reason: str | None = None,
) -> None:
    """Central workflow_paused emit helper. `interrupt_reason` (coord.pause_reason)
    rides alongside the engine-classified `reason` when a user interrupt drove
    the pause — the live 'interrupted (esc)' marker renders from it (§2)."""
    payload: dict[str, Any] = {"reason": reason}
    if kind is not None:
        payload["kind"] = kind
    if step is not None:
        payload["step"] = step
    if interrupt_reason is not None:
        payload["interrupt_reason"] = interrupt_reason
    _emit(log, "workflow_paused", payload)


def _build_sandbox_policy(change: Change, config: Any, log: EventLog) -> Any:
    """Resolve the SandboxPolicy once per run (IMP_* are change-stable, so one
    build matches every spawn point). Emit a durable posture event; on a hard-fail
    posture, emit + re-raise so the run stops rather than silently running bare."""
    raw = config.sandbox or {}
    cfg = SandboxConfig(
        enabled=raw.get("enabled", True),
        allow_unsandboxed=raw.get("allow_unsandboxed", False),
        network=raw.get("network", True),
        secret_paths=tuple(raw.get("secret_paths", ())),
        writable_caches=tuple(raw.get("writable_caches", ())),
        extra_readable=tuple(raw.get("extra_readable", ())),
        extra_writable=tuple(raw.get("extra_writable", ())),
        extra_env=tuple(raw.get("extra_env", ())),
    )
    eng = overlay_engine_env(
        os.environ,
        engine_env(project_root=change.project_root, change_id=change.id,
                   change_handle=change.handle, change_title=change.title,
                   verify_command=config.verify_command,
                   verify_timeout=config.verify_timeout),
    )
    try:
        policy = resolve_policy(
            worktree=change.project_root, control_root=change.control_root,
            change_id=change.id, cfg=cfg, engine_env_result=eng,
            environ=os.environ, available=sandbox_available(),
        )
    except SandboxUnavailable as exc:
        _emit(log, "sandbox_unavailable", {"message": str(exc)}, agent_id=None)
        raise
    if not policy.active:
        _emit(log, "sandbox_inactive", {"reason": policy.note}, agent_id=None)
    return policy
