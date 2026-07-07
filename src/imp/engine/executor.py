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

import dataclasses
import functools
import json
import os
import shutil
import sys
import tempfile
import traceback
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from imp.agents.prompts import build_context, render_label, render_prompt
from imp.agents.random_status import STATUS_PHRASE_LABEL
from imp.agents.runner import (
    ChangeContext,
    Message,
)
from imp.agents.skills import overlay_project_skills
from imp.engine import test_lane as _test_lane
from imp.engine.agent_run import (
    AgentRunResult,
    build_agent_invocation,
    drive_invocation,
)
from imp.engine.backoff import (
    interruptible_sleep as _interruptible_sleep,
)
from imp.engine.change import Change
from imp.engine.env import TestGates, engine_env, overlay_engine_env
from imp.engine.error_policy import budget_for
from imp.engine.errors import LoadError, summarize_exception
from imp.engine.feedback import (
    apply_completion_backstop,
    format_clean_tree_feedback,
    format_script_feedback,
    render_output_fields,
    summarize_transition_reason,
)
from imp.engine.gates import (
    RuleMatch,
    ScriptInterrupted,
    evaluate_rules,
    run_script,
)
from imp.engine.loader import load_workflow
from imp.engine.reincarnation import reincarnation_seed, stash_transcript
from imp.engine.sandbox import (
    SandboxUnavailable,
    resolve_policy,
    sandbox_available,
    sandbox_config_from_raw,
)
from imp.engine.schema import validate_agent_output
from imp.engine.state import CurrentStep, EventLog, JsonlEventLog, State
from imp.engine.types import (
    Action,
    Continue,
    Goto,
    OutputSchema,
    Pause,
    Retry,
    Step,
    Workflow,
)
from imp.providers.effective import load_effective_config
from imp.providers.resolve import provider_from_model, resolve_run

if TYPE_CHECKING:
    from imp.providers.config import Config

EventCallback = Callable[[dict[str, Any]], None]

OUTCOME_HISTORY_K = 4


def _record_outcome(
    state: State, step_name: str, output: dict[str, Any], head: str,
) -> None:
    outcome = output.get("outcome")
    assert isinstance(outcome, str), f"outcome must be str; got {outcome!r}"
    hist = state.outcome_history.setdefault(step_name, [])
    next_lap = hist[-1]["lap"] + 1 if hist else 1
    hist.append({"lap": next_lap, "outcome": outcome, "head": head})
    del hist[:-OUTCOME_HISTORY_K]


def _worktree_head(change: Change) -> str:
    """Worktree HEAD sha, or "" when HEAD is absent/unborn (like change._imp_commit)."""
    from imp.engine import worktree
    proc = worktree._git(change.project_root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


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
    rebase skips the reconciler, then integrate-verify fails and resumes it), and
    passively replaying nothing builds an empty query that hangs
    receive_response() forever."""
    from imp.agents.transcript import Transcript
    return Transcript(transcript_dir / "transcript.jsonl").exists_and_nonempty()



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


def _trees_identical(a: Path, b: Path) -> bool:
    """True iff both trees contain the same set of relative file paths with
    byte-identical contents. Empty directories are ignored — the snapshot's
    meaning is its files."""
    files_a = {p.relative_to(a): p for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b): p for p in b.rglob("*") if p.is_file()}
    if set(files_a) != set(files_b):
        return False
    return all(
        files_a[rel].read_bytes() == files_b[rel].read_bytes()
        for rel in files_a
    )


def _refresh_workflow_snapshot(change: Change) -> bool:
    """Re-snapshot the live workflow into change_dir/workflow at a run_change
    entry. Returns True only when an EXISTING snapshot's content was replaced
    (a fresh change's initial copy returns False); best-effort and crash-safe."""
    change_dir = change.change_dir
    snapshot = change_dir / "workflow"
    tmp = change_dir / "workflow.tmp"
    old = change_dir / "workflow.old"

    # 1. Recover crash artifacts from a prior interrupted swap.
    if tmp.exists():
        shutil.rmtree(tmp)
    if not snapshot.exists() and old.exists():
        os.rename(old, snapshot)          # crash mid-swap: restore the snapshot
    elif old.exists():
        shutil.rmtree(old)                # leftover backup: drop it

    had_snapshot = snapshot.is_dir()

    # 2. Resolve the live source; best-effort — never introduce a new failure.
    try:
        live = change.live_workflow_dir
    except Exception:
        return False
    if not live.is_dir():
        return False

    # 3. Build the candidate exactly as the initial snapshot does.
    shutil.copytree(live, tmp)
    overlay_project_skills(change.project_root, tmp / "skills" / "user")

    # 4. No-op when the candidate matches the current snapshot.
    if had_snapshot and _trees_identical(snapshot, tmp):
        shutil.rmtree(tmp)
        return False

    # 5. Swap crash-safely (step 1 makes each intermediate state recoverable).
    if had_snapshot:
        os.rename(snapshot, old)
        os.rename(tmp, snapshot)
        shutil.rmtree(old)
    else:
        os.rename(tmp, snapshot)

    return had_snapshot


def _reconcile_resolved_run(
    state: State, workflow: Workflow, config: Config,
) -> bool:
    """Merge into the persisted resolution maps only the tier/step keys the
    refreshed workflow introduced (keys absent from the current maps), so a
    pinned model survives; returns True iff a key was added (caller persists)."""
    fresh_tiers, fresh_steps, _details = resolve_run(workflow, config)
    assert state.resolved_tiers is not None and state.resolved_steps is not None
    changed = False
    for tier, pm in fresh_tiers.items():
        if tier not in state.resolved_tiers:
            state.resolved_tiers[tier] = pm
            changed = True
    for step_name, pm in fresh_steps.items():
        if step_name not in state.resolved_steps:
            state.resolved_steps[step_name] = pm
            changed = True
    return changed


async def run_change(
    change: Change,
    *,
    on_event: EventCallback | None = None,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,  # cli.signals.InterruptCoordinator | None
    headless: bool = False,
    autonomous: bool = False,
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
        workflow_refreshed = _refresh_workflow_snapshot(change)
        config = load_effective_config(
            project_root=change.project_root, control_root=change.control_root,
        ).config
        state = State.load(change.state_dir)
        change.relink_home()
        # Transient: re-set on EVERY run_change entry (fresh + resume) so the
        # Tier A handoff watch always sees the configured threshold without
        # persisting it (the resolved_tiers block below is skipped on resume).
        state.checkpoint_threshold = config.context_checkpoint_threshold()
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
        if workflow_refreshed:
            _emit(log, "workflow_refreshed", {}, agent_id=None)
        sandbox_policy = _build_sandbox_policy(change, config, log)
        policy_root = change.project_root

        # Resolve tiers once per run and persist, so resume never re-snapshots the
        # environment (design "When it runs"). run_change re-executes on every
        # resume, so guard on the persisted map.
        if state.resolved_tiers is None:
            resolved_tiers, resolved_steps, details = resolve_run(workflow, config)
            state.resolved_tiers = resolved_tiers
            state.resolved_steps = resolved_steps
            state.max_reincarnations = config.context_max_reincarnations()
            state.save(change.state_dir)
            _emit(log, "tiers_resolved",
                  {"tiers": details["tiers"], "steps": details["steps"],
                   "checkpoint_threshold": config.context_checkpoint_threshold()},
                  agent_id=None)
            if seed_opening:
                _emit(log, "opening_intent", {"text": seed_opening},
                      agent_id=None)
        elif workflow_refreshed:
            if _reconcile_resolved_run(state, workflow, config):
                state.save(change.state_dir)

        flat = workflow.flat_steps()
        index_of = {s.name: i for i, s in enumerate(flat)}
        pending_seed = seed_opening  # one-shot: consumed by the first fresh agent step

        # A hold mark surviving to process entry means a prior run died while holding
        # (a clean pause clears the mark). Don't evict when the merge already succeeded
        # (merge_commit set) or the run is parked at completion — those finalize via
        # the idempotent merge path / _complete.
        if (state.held_lane is not None
                and not state.completion_pending
                and not state.merge_commit):
            _evict_if_holding(change, state, log)
            state.save(change.state_dir)

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
                # (corruption, or a refresh that dropped this step). Never
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
            # Go-live mid-run flips change.project_root to the freshly-forked
            # worktree; the once-built policy is then rooted at the stale control
            # root. Rebuild so the next step's gates and agents bind the worktree.
            if change.project_root != policy_root:
                sandbox_policy = _build_sandbox_policy(change, config, log)
                policy_root = change.project_root
            # var_mutations was ALREADY applied to state.vars and logged inside the
            # _route_* body — run_change receives it for visibility only and must NOT
            # re-apply it (double-apply trap). The third element is otherwise unused.
            action, feedback_text, _var_mutations = await _dispatch_step(
                step, workflow, change, state, log, is_resume=is_resume,
                user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
                test_gates=TestGates.from_config(config),
                headless=headless,
                autonomous=autonomous,
                sandbox_policy=sandbox_policy,
                seed_opening=seed_for_step,
                project_facts=state.project_facts or {},
                guidance=state.guidance or "",
                wait_for_turn_fn=wait_for_turn_fn,
                max_parallel_test_runs=config.max_parallel_test_runs)
            if seed_for_step is not None:
                pending_seed = None

            if isinstance(action, Retry):
                raise AssertionError("Retry must not escape _dispatch_step")

            # §3 case 1 — mid-flight Pause: pause BEFORE navigation (as today).
            if isinstance(action, Pause):
                evicted = _evict_if_holding(change, state, log)
                if not evicted and step.kind == "agent":
                    state.arm_step_resume(
                        interrupt=coordinator.should_pause or action.interrupt)
                    # Keep on transient give-up; clear on genuine yield or any other park.
                    state.retry_continue_pending = action.reason in (
                        "retry_ceiling", "reauth_needed")
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
                    state.interrupt_pending = False
                    state.retry_continue_pending = False
                    return _complete_or_park(change, state, log, coordinator)
                state.step = flat[nxt].name
                state.step_label = (
                    render_label(flat[nxt].label, change, log, state)
                    or flat[nxt].name
                )
                state.resume_pending = None
                state.interrupt_pending = False
                state.retry_continue_pending = False
            elif isinstance(action, Goto):
                if action.release is not None and state.held_lane is not None:
                    _release_held_lane(
                        action.release, change, state, log, step_name=step.name)
                target_idx = workflow.resolve_goto(action.target)
                if target_idx >= len(flat):                      # empty trailing phase
                    state.interrupt_pending = False
                    state.retry_continue_pending = False
                    return _complete_or_park(change, state, log, coordinator)
                if target_idx <= idx:                            # backward OR self → loop-counted
                    n = state.loop_counts.get(step.name, 0) + 1
                    state.loop_counts[step.name] = n
                    if n > step.max_loops and not coordinator.should_pause:
                        _evict_if_holding(change, state, log)
                        state.status = "paused"
                        state.save(change.state_dir)
                        _emit_paused_event(log, "max_loops", step=step.name)
                        return RunResult.PAUSED
                else:
                    state.loop_counts.pop(step.name, None)       # forward goto resets
                state.step = flat[target_idx].name
                state.step_label = (
                    render_label(flat[target_idx].label, change, log, state)
                    or flat[target_idx].name
                )
                if action.resume:
                    # Single-shot reset: a resumed step must not inherit a stale
                    # retry count. Done HERE (the navigation point that sets
                    # resume_pending), not in _route_agent — is_resume stays True
                    # across internal retries, so resetting per entry would let a
                    # persistent error retry forever.
                    state.step_attempt = 0
                    state.resume_pending = flat[target_idx].name
                    state.interrupt_pending = False
                    state.retry_continue_pending = False
                else:
                    state.resume_pending = None
                    state.interrupt_pending = False
                    state.retry_continue_pending = False
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

            # Backward Goto (strictly-earlier target) = a bounce: emit a generic
            # marker (no step/gate name) so imp log renders it legibly.
            if isinstance(action, Goto) and target_idx is not None and target_idx < idx:
                _emit(log, "step_transition", {
                    "from_step": step.name,
                    "target_step": flat[target_idx].name,
                    "backward": True,
                    "reason": summarize_transition_reason(feedback_text),
                })

            # §3 case 2 — an in-flight interrupt pauses AFTER navigation, so
            # re-entry starts the navigated-to step and §2 pending texts reach it.
            # Navigation already set resume_pending correctly (Goto(resume=True)
            # kept the target; Continue/forward-Goto cleared it) — do NOT override.
            if coordinator.should_pause:
                _evict_if_holding(change, state, log)
                state.status = "paused"
                state.save(change.state_dir)
                _emit_paused_event(
                    log, "interrupt", interrupt_reason=coordinator.pause_reason)
                return RunResult.PAUSED
            state.save(change.state_dir)
    except Exception as exc:
        # Last-resort guard: except Exception (not BaseException) pauses resumably on an
        # engine bug, letting anyio cancellation / Ctrl-C propagate.
        tb = traceback.format_exc()
        try:
            kind = type(exc).__name__
            message, extras = summarize_exception(exc)
            log.append(
                agent_id=None, type="error",
                payload={
                    "category": "internal",
                    "kind": kind,
                    "message": message,
                    "detail": {
                        "traceback_tail": "\n".join(tb.splitlines()[-15:]),
                        **extras,
                    },
                },
            )
            _emit_paused_event(log, "internal_error", kind=kind)
            state.resume_pending = state.step
            state.status = "paused"
            state.save(change.state_dir)
        except Exception:
            # Best-effort: if the emit/save fails (or log/state was never bound), re-raise
            # the original (from None) so daemon.stderr surfaces it, not a spurious
            # UnboundLocalError.
            raise exc from None
        print(tb, file=sys.stderr)
        return RunResult.PAUSED
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
    - merge-to-base already merged and exited the worktree (state.merge_commit set,
      base ref durably advanced): finalize — mark completed, archive the final
      snapshot. Ordering is crash-safe: status is written + saved BEFORE archive,
      so a crash mid-finalize leaves the merge durable. Recovery: a crash AFTER
      status='completed' is already done; a crash in the window between
      _route_merge_and_exit_worktree recording merge_commit and this status write
      leaves status='running' — re-running re-enters the merge step, which
      short-circuits on state.merge_commit (idempotent, no second merge) and
      completes cleanly.

    _complete no longer does `git merge` or worktree teardown — the authoritative
    merge and worktree exit moved into the routable merge-to-base step (B3/F1),
    and the H1 dirty net moved into merge_to_base under the lock (R5)."""
    state.held_lane = None
    state.held_acquire_step = None
    state.step = None
    state.step_label = None
    state.route_label = None
    from imp.engine import merge_queue
    if not state.merge_commit:
        merge_queue.pop_and_wake(change.control_root, change.id, change.base_branch)
        state.status = "completed"
        state.save(change.state_dir)
        _emit(log, "workflow_completed", {})
        shutil.rmtree(change.change_dir / "steps", ignore_errors=True)
        return RunResult.COMPLETED

    state.status = "completed"
    state.save(change.state_dir)
    _emit(log, "change_merged",
          {"base": change.base_branch, "commit": state.merge_commit})
    merge_queue.pop_and_wake(change.control_root, change.id, change.base_branch)
    from imp.engine import archive
    archive.archive_change(
        change.control_root, change.id,
        events_path=change.events_path,
        change_json_path=change.change_dir / "change.json",
        state_json_path=change.state_dir / "state.json",
    )
    shutil.rmtree(change.change_dir, ignore_errors=True)
    return RunResult.COMPLETED


def _apply_readonly_overlay(policy: Any, step: Step, project_root: Path) -> Any:
    """Compose step.read_only into a dispatch-time policy copy; no-op if empty or None."""
    if not step.read_only or policy is None:
        return policy
    overlays = tuple(str((project_root / p).resolve()) for p in step.read_only)
    return dataclasses.replace(policy, readonly_overlays=overlays)


async def _dispatch_step(
    step: Step, workflow: Workflow, change: Change,
    state: State, log: EventLog, *, is_resume: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
    test_gates: TestGates | None = None,
    headless: bool = False,
    autonomous: bool = False,
    sandbox_policy: Any = None,
    seed_opening: str | None = None,
    project_facts: dict[str, str] | None = None,
    guidance: str = "",
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None = None,
    max_parallel_test_runs: int = 1,
) -> tuple[Action, str | None, dict[str, object]]:
    """Returns (action, feedback_text, var_mutations)."""
    wrapper_text: str | None = None
    fb = state.retry_feedback
    if fb is not None and fb.get("target_step") == step.name:
        wrapper_text = fb.get("text")
        state.retry_feedback = None
        state.save(change.state_dir)

    if step.release is not None:
        _release_lane(step, change, state, log)
    if step.acquire is not None and step.acquire != "test-lane":
        return await _route_acquire(step, change, state, log, wait_for_turn_fn)

    sandbox_policy = _apply_readonly_overlay(sandbox_policy, step, change.project_root)

    if step.kind == "branch":
        return _route_branch(step, change, state, log)
    if step.kind == "merge_and_exit_worktree":
        return await _route_merge_and_exit_worktree(step, change, state, log)
    if step.kind == "script":
        return await _route_script(step, change, state, log,
                                   test_gates=test_gates,
                                   sandbox_policy=sandbox_policy,
                                   max_parallel_test_runs=max_parallel_test_runs,
                                   coordinator=coordinator)
    return await _route_agent(
        step, workflow, change, state, log,
        user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
        wrapper_text=wrapper_text, is_resume=is_resume, test_gates=test_gates,
        headless=headless,
        autonomous=autonomous,
        sandbox_policy=sandbox_policy, seed_opening=seed_opening,
        project_facts=project_facts, guidance=guidance)


def _step_started_payload(
    step: Step, state: State,
    change: Change | None = None, log: EventLog | None = None,
) -> dict[str, Any]:
    payload = {"step": step.name, "kind": step.kind, "phase": step.phase,
               "show_label": step.show_label,
               "show_output": step.show_output,
               "label": render_label(step.label, change, log, state)}
    if step.label == STATUS_PHRASE_LABEL:
        payload["rotate_phrase"] = True
    if step.kind == "script":
        payload["command"] = step.command
    if step.position_label:
        payload["position_label"] = True
    if step.phase_label is not None:
        payload["phase_label"] = render_label(step.phase_label, change, log, state)
    if not step.phase_show_label:
        payload["phase_show_label"] = False
    # Consume-on-read: transfer route_label from the prior step's _finish, then clear it.
    if state.route_label is not None:
        payload["route_label"] = state.route_label
        state.route_label = None
    return payload


def _emit_started(
    log: EventLog, step: Step, state: State, change: Change, **extra: Any,
) -> dict[str, Any]:
    return _emit(log, "step_started",
                 {**_step_started_payload(step, state, change, log), **extra},
                 agent_id=None)


def _harvest_artifacts(
    change: Change, step: Step, warn: Callable[[str], None] | None = None
) -> None:
    """Journal change_dir/artifacts/** to the change's artifact ref (no-op if none)."""
    from imp.engine import worktree
    art_root = change.change_dir / "artifacts"
    if not art_root.is_dir():
        return
    files = {
        str(p.relative_to(art_root)): p
        for p in sorted(art_root.rglob("*")) if p.is_file()
    }
    worktree.artifact_commit(
        change.control_root, change.id, files,
        message=f"artifact: {step.name}", warn=warn)


def _finish(
    log: EventLog, step: Step, change: Change, match: Any, state: State
) -> tuple[Action, str | None, dict[str, object]]:
    def _warn(msg: str) -> None:
        _emit(log, "warning",
              {"source": "artifact_harvest", "message": msg, "detail": {}})
    # except Exception, not BaseException: lets CancelledError / Ctrl-C propagate.
    try:
        _harvest_artifacts(change, step, warn=_warn)
    except Exception as e:
        msg, extras = summarize_exception(e)
        _emit(log, "warning",
              {"source": "artifact_harvest", "message": msg, "detail": extras})
    _emit(log, "step_completed", {
        "step": step.name, "kind": step.kind,
        "action": _action_payload(match.action),
        "var_mutations": match.var_mutations,
        "label": match.rule.label if match.rule is not None else None,
    })
    # Thread the label to the next step_started; None-write clears a prior labeled route.
    state.route_label = match.rule.label if match.rule is not None else None
    return match.action, None, match.var_mutations


def _route_branch(
    step: Step, change: Change, state: State, log: EventLog
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step, state, change)
    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, change, match, state)            # branch goto carries NO feedback


class ScriptOutputError(Exception):
    """A script with a declared output: failed to produce a valid $IMP_OUTPUT."""


def _read_script_output(output_path: Path, schema: OutputSchema) -> dict[str, Any]:
    try:
        raw = output_path.read_text()
    except OSError as e:
        raise ScriptOutputError(f"script did not write $IMP_OUTPUT: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ScriptOutputError(f"$IMP_OUTPUT is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ScriptOutputError("$IMP_OUTPUT must be a JSON object")
    try:
        validate_agent_output(data, schema)
    except LoadError as e:
        raise ScriptOutputError(str(e)) from e
    return {f.name: data[f.name] for f in schema.fields if f.name in data}


async def _route_script(
    step: Step, change: Change, state: State, log: EventLog,
    *, test_gates: TestGates | None = None,
    sandbox_policy: Any = None,
    max_parallel_test_runs: int = 1,
    coordinator: Any = None,
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step, state, change)
    command = step.command
    assert command is not None, "script step must have a command"
    if "{{" in command:
        command = render_prompt(command, build_context(
            change_title=change.title, event_log=log,
            change_id=change.id, change_home=f".imp/changes/{change.id}",
            cur_base_head=change.cur_base_head,
            orig_base_head=change.orig_base_head, vars=state.vars,
        ))
    # The script gate's subprocess runs to completion under a blocking
    # proc.communicate() (gates.run_script). Offload it to a worker thread so the
    # event loop keeps turning — otherwise a multi-minute verify gate starves
    # every concurrent task on the loop (the live-status tick() animator, the
    # signal receiver, the remote-channel poller), freezing the UI at
    # "running · 0s" until the command returns. Only the blocking wait moves off
    # the loop; the event emits stay on the loop thread (single terminal writer).
    if step.interruptible:
        assert coordinator is not None, \
            "interruptible step requires a coordinator to read should_pause"

    def _abort() -> bool:
        return bool(coordinator.should_pause)

    abort = _abort if step.interruptible else None
    output_path = None
    if step.output_schema is not None:
        fd, tmp = tempfile.mkstemp(prefix="imp-output-", suffix=".json")
        os.close(fd)
        output_path = Path(tmp)
    slot_fd: int | None = None
    try:
        if step.acquire == "test-lane":
            slot_fd = _test_lane.try_acquire(change.control_root, max_parallel_test_runs)
            if slot_fd is None:
                _emit(log, "waiting_for_test_lane", {"step": step.name})
                slot_fd = await anyio.to_thread.run_sync(
                    functools.partial(_test_lane.acquire_blocking,
                                      change.control_root, max_parallel_test_runs))
                _emit(log, "test_lane_acquired", {"step": step.name})
        out = await anyio.to_thread.run_sync(
            functools.partial(
                run_script, step, cwd=change.workflow_dir,
                env_extra=engine_env(project_root=change.project_root,
                                     change_id=change.id,
                                     change_title=change.title,
                                     change_dir=change.change_dir,
                                     output_path=output_path,
                                     test_gates=test_gates),
                policy=sandbox_policy, command=command, abort=abort))
        if output_path is not None:
            assert step.output_schema is not None
            try:
                out = {**out, **_read_script_output(output_path, step.output_schema)}
            except ScriptOutputError as e:
                _finish(log, step, change,
                        RuleMatch(action=Pause(reason="script_blocked"),
                                  rule=None, var_mutations={}), state)
                _emit(log, "script_blocked", {
                    "step": step.name, "exit": out["exit"],
                    "stdout": out["stdout"], "stderr": str(e)})
                return Pause(reason="script_blocked"), str(e), {}
    except ScriptInterrupted:
        return Pause(interrupt=True), None, {}
    finally:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        if slot_fd is not None:
            _test_lane.release(slot_fd)
    if step.show_output and out["exit"] == 0:
        _emit(log, "script_output", {"step": step.name, "stdout": out["stdout"]})
    match = evaluate_rules(step.rules, vars=state.vars, output=out)
    state.vars.update(match.var_mutations)
    action, _, muts = _finish(log, step, change, match, state)
    feedback = None
    if isinstance(action, Goto):
        # exit-0 Goto is a success forward-route; only non-zero exit is a failure-retry.
        if out["exit"] != 0:
            feedback = format_script_feedback(
                command, out["exit"], out["stdout"], out["stderr"])
    elif isinstance(action, Pause):
        # Script pauses (e.g. integrate-rebase exit 3/4/5,
        # cleanup-merge/assert-stamped) otherwise lose their stderr — the pause verb
        # carries no reason and the CLI keys remediation only on merge_blocked.
        # Journal it so `imp log` shows it and the CLI can surface it (findings #5/#6).
        _emit(log, "script_blocked", {
            "step": step.name, "exit": out["exit"],
            "stdout": out["stdout"], "stderr": out["stderr"]})
        action = Pause(reason="script_blocked")
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
    from imp.engine.awaiting_user_input import AwaitingUserInput
    if predicate():
        return
    raise AwaitingUserInput()


def _resolve_lane(value: str, change: Change) -> str:
    if value == "merge-lane":
        return change.base_branch
    raise RuntimeError(f"unknown lane keyword {value!r}")


async def _route_acquire(
    step: Step, change: Change, state: State, log: EventLog,
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None,
) -> tuple[Action, str | None, dict[str, object]]:
    """Enqueue in lane; proceed as head, marking the hold; hibernate if unattended non-head."""
    from imp.engine import merge_queue
    from imp.engine.awaiting_user_input import AwaitingUserInput
    if wait_for_turn_fn is None:
        raise RuntimeError("wait_for_turn_fn is required for acquire steps")
    assert step.acquire is not None
    lane = _resolve_lane(step.acquire, change)
    _emit_started(log, step, state, change)
    merge_queue.enqueue_if_absent(change.control_root, change.id, lane)

    def _is_head() -> bool:
        return merge_queue.is_head(change.control_root, change.id, lane)

    if not _is_head():
        ahead = merge_queue.position(change.control_root, change.id, lane)
        _emit(log, "lane_waiting", {"base": lane, "ahead": ahead})
        last_ahead = ahead

        def _poll_turn() -> bool:
            nonlocal last_ahead
            current = merge_queue.position(change.control_root, change.id, lane)
            if current != last_ahead:
                _emit(log, "lane_waiting", {"base": lane, "ahead": current})
                last_ahead = current
            return _is_head()

        try:
            await wait_for_turn_fn(_poll_turn)
        except AwaitingUserInput:
            pause = Pause(reason="awaiting_turn")
            _emit(log, "step_completed", {
                "step": step.name, "kind": step.kind,
                "action": _action_payload(pause), "var_mutations": {}})
            return pause, None, {}

    state.held_lane = lane
    state.held_acquire_step = step.name
    state.save(change.state_dir)
    _emit(log, "lane_acquired", {"base": lane, "step": step.name, "glyph": step.glyph})
    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, change, match, state)


def _release_held_lane(
    lane_keyword: str, change: Change, state: State, log: EventLog, *, step_name: str,
) -> None:
    """Pop the held lane and clear the hold mark, waking the next head."""
    from imp.engine import merge_queue
    lane = _resolve_lane(lane_keyword, change)
    merge_queue.pop_and_wake(change.control_root, change.id, lane)
    state.held_lane = None
    state.held_acquire_step = None
    state.save(change.state_dir)
    _emit(log, "lane_released", {"base": lane, "step": step_name})


def _release_lane(step: Step, change: Change, state: State, log: EventLog) -> None:
    """Pop the lane before the step body, clearing the hold mark."""
    assert step.release is not None
    _release_held_lane(step.release, change, state, log, step_name=step.name)


def _evict_if_holding(change: Change, state: State, log: EventLog) -> bool:
    """Release a held lane on pause/crash and rewind the resume cursor to the acquiring step."""
    if state.held_lane is None:
        return False
    from imp.engine import merge_queue
    lane = state.held_lane
    acquire_step = state.held_acquire_step
    merge_queue.pop_and_wake(change.control_root, change.id, lane)
    state.held_lane = None
    state.held_acquire_step = None
    state.step = acquire_step
    state.resume_pending = None
    _emit(log, "lane_evicted", {"base": lane, "rewind_to": acquire_step})
    return True


def _exit_worktree(change: Change) -> None:
    """Idempotent: remove_worktree(force=True) tolerates an already-absent worktree."""
    from imp.engine import worktree
    worktree.remove_worktree(change.control_root, change.id, force=True)
    if change.project_root != change.control_root:
        change.set_project_root(change.control_root)


def _merge_blocked_pause(
    log: EventLog, step: Step, change: Change, status: str, detail: str | None,
    *, note: str | None = None,
) -> tuple[Action, str | None, dict[str, object]]:
    reason = "worktree_dirty" if status == "DIRTY" else status.lower()
    payload: dict[str, Any] = {"reason": reason, "base": change.base_branch, "detail": detail}
    if note is not None:
        payload["note"] = note
    _emit(log, "merge_blocked", payload)
    pause = Pause(reason="merge_blocked", kind=reason)
    _emit(log, "step_completed",
          {"step": step.name, "kind": "merge_and_exit_worktree",
           "action": _action_payload(pause), "var_mutations": {}})
    return pause, None, {}


async def _route_merge_and_exit_worktree(
    step: Step, change: Change, state: State, log: EventLog,
) -> tuple[Action, str | None, dict[str, object]]:
    from imp.engine import worktree
    _emit_started(log, step, state, change)

    if state.merge_commit:
        _exit_worktree(change)
        match = evaluate_rules(step.rules, vars=state.vars, output={"merge": "MERGED"})
        state.vars.update(match.var_mutations)
        return _finish(log, step, change, match, state)

    spec_path = change.change_dir / "artifacts" / "spec" / f"{change.id}.md"
    body = _decisions_body(spec_path)
    result = await anyio.to_thread.run_sync(
        functools.partial(
            worktree.merge_to_base,
            change.control_root, change.id, change.base_branch, title=change.title,
            body=body, project_root=change.project_root))
    if result.status == "MERGED":
        state.merge_commit = result.merge_commit
        state.save(change.state_dir)
        _exit_worktree(change)

    match = evaluate_rules(step.rules, vars=state.vars, output={"merge": result.status})

    if isinstance(match.action, Goto):                # a recovery route
        attempt = state.loop_counts.get(step.name, 0)
        if attempt >= step.max_loops:                 # exhausted — pause one attempt early
            note = (f"imp attempted {step.max_loops} recovery passes and the "
                    "merge still failed.")
            return _merge_blocked_pause(
                log, step, change, result.status, result.detail, note=note)
        _emit(log, "merge_retry",
              {"status": result.status, "attempt": attempt + 1,
               "max": step.max_loops, "base": change.base_branch})
        state.vars.update(match.var_mutations)
        action, _, muts = _finish(log, step, change, match, state)
        # _finish hardcodes its feedback element to None; thread the rule's own
        # feedback so a DIRTY route's preamble reaches the reconciler (mirrors the
        # agent route).
        return action, match.action.feedback, muts

    if isinstance(match.action, Pause):               # REFUSED or any other status
        return _merge_blocked_pause(log, step, change, result.status, result.detail)

    state.vars.update(match.var_mutations)
    return _finish(log, step, change, match, state)


def _enforce_clean_tree(
    step: Step, change: Change, state: State, log: EventLog,
) -> tuple[Action, str | None, dict[str, object]] | None:
    """Post-step clean-tree invariant: None to proceed to rules, else a
    bounce-or-pause (action, feedback, muts) tuple."""
    from imp.engine import worktree

    key = f"step:{step.phase}/{step.name}"
    # Opt-out, or no dedicated git worktree at project_root (draft: == control
    # root; or a project_root that is not a real checkout, e.g. a fake/flipped
    # test root) => the invariant does not apply. The is_worktree guard also
    # keeps the hook from raising `git status` on a non-repo path.
    if (not step.clean_tree
            or change.project_root == change.control_root
            or not worktree.is_worktree(change.project_root)):
        state.clean_tree_bounces.pop(key, None)
        return None
    paths = worktree.uncommitted_paths(change.project_root)
    if not paths:
        state.clean_tree_bounces.pop(key, None)
        return None
    if state.clean_tree_bounce_count(key) >= state.max_clean_tree_bounces:
        pause = Pause(reason="clean_tree", kind="dirty_worktree_cap")
        _emit(log, "step_completed",
              {"step": step.name, "kind": "agent",
               "action": _action_payload(pause), "var_mutations": {}})
        return pause, None, {}
    state.bump_clean_tree_bounce(key)
    state.current_step = None
    action = Goto(target=step.name, resume=True)
    # Journal the bounce: navigation emits step_transition only for a strictly
    # backward target, and this self-goto (target == current step) bypasses
    # _finish, so without this the bounce leaves no event between the two
    # step_started records. Mirror the cap-pause branch's step_completed emit.
    _emit(log, "step_completed",
          {"step": step.name, "kind": "agent",
           "action": _action_payload(action), "var_mutations": {}})
    state.save(change.state_dir)
    return action, format_clean_tree_feedback(paths), {}


async def _route_agent(
    step: Step, workflow: Workflow, change: Change,
    state: State, log: EventLog, *,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, wrapper_text: str | None, is_resume: bool,
    test_gates: TestGates | None = None,
    headless: bool = False,  # forwarded to the invocation/child runs
    autonomous: bool = False,
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
    # Plain lifecycle bit: True only on an empty-transcript process-resume (restart from scratch).
    restarted = is_resume and not keep
    # Backstop applied once here; the raw parameter is what recursive re-entries re-apply.
    delivered_wrapper = apply_completion_backstop(
        wrapper_text, has_schema=bool(agent_def.output_schema.fields))
    run_opening = ""
    feedback_lead: str | None = None
    if not keep:
        shutil.rmtree(transcript_dir, ignore_errors=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = False
        retry_wrapper: str | None = None
        # seed_opening (imp new intent) renders via run.opening in templates.
        # wrapper_text (goto/rework feedback) is not rendered by the target's
        # template, so it is delivered as the lead of the relocated user message.
        run_opening = seed_opening or ""
        feedback_lead = delivered_wrapper if seed_opening is None else None
        initial: tuple[Message, ...] = ()
    else:  # keep the transcript, balance a trailing complete_step
        transcript_dir.mkdir(parents=True, exist_ok=True)
        balance = True
        # No opener on resume: providers replay the on-disk transcript and
        # never iterate initial_messages. retry_wrapper rides on
        # retry_feedback (balance_success_on_resume), a separate channel.
        retry_wrapper = delivered_wrapper
        initial = ()

    state.current_step = CurrentStep(transcript_dir=transcript_dir)
    state.save(change.state_dir)

    backoff = 0
    cumulative_backoff = 0.0
    hint_exempt_sleeps = 0
    while True:
        result = await _run_agent_invocation(
            agent_id=f"step:{step.phase}/{step.name}", agent_def=agent_def,
            tier=step.tier, step_name=step.name,
            workflow=workflow, change=change, state=state, log=log,
            transcript_dir=transcript_dir,
            step_started_payload_extras={
                **_step_started_payload(step, state, change, log),
                "resumed": is_resume, "restarted": restarted},
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator, prior_backoff_attempts=backoff,
            retry_feedback=retry_wrapper if backoff == 0 else None,
            balance_on_resume=balance,
            # is_resume=keep, not the entry's lifecycle bit: the stored-prompt
            # restore (system_prompt_override) suppresses first-user-message
            # composition on the assumption a kept transcript replays it. A
            # restart (empty transcript, e.g. a watchdog kill before the
            # provider's result-boundary flush) has a stored step_context but
            # nothing to replay — restoring would stream no messages at all and
            # the spawn hangs silently until the next watchdog kill.
            initial_messages=initial, is_resume=keep,
            test_gates=test_gates, headless=headless,
            interactive=step.interactive,
            autonomous=autonomous,
            sandbox_policy=sandbox_policy,
            project_facts=project_facts, guidance=guidance,
            opening=run_opening, feedback_lead=feedback_lead)
        if result.delay is None:
            break
        # Per-category wall-clock budget: pause cleanly when cumulative
        # transient-error backoff would exceed this category's budget. Checked
        # BEFORE the agent_retry emit so the journal shows a clean ceiling pause,
        # not a retry-then-pause. Identical in interactive and headless runs.
        # delay is non-None only for RetryWithBackoff categories, which always
        # carry a budget, so budget is non-None here.
        category = result.error.category if result.error else "unknown"
        # A hinted delay is an ANNOUNCED wait (a parsed reset time > 60s), not
        # blind retrying: exempt it from the budget and don't accrue it. Capped at
        # 3 such sleeps per loop so a pathological hint stream still terminates —
        # the 4th hinted delay is budgeted like any other.
        hinted = result.delay_hinted and hint_exempt_sleeps < 3
        if not hinted:
            budget = budget_for(category)
            if budget is not None and cumulative_backoff + result.delay > budget:
                # Auth exhaustion is actionable: the human must re-authenticate, not wait out a
                # generic ceiling.
                reason = "reauth_needed" if category == "auth" else "retry_ceiling"
                pause = Pause(reason=reason,
                              kind=result.error.kind if result.error else None)
                _emit(log, "step_completed",
                      {"step": step.name, "kind": "agent",
                       "action": _action_payload(pause), "var_mutations": {}})
                return pause, None, {}
        backoff += 1
        # Persist before sleep so a kill/crash during backoff leaves the intent on disk.
        state.retry_continue_pending = True
        state.save(change.state_dir)
        # Self-healed-retry visibility (agent_retry): surface the backoff so a
        # transient network/rate_limit error that recovers doesn't look like a
        # failure. category comes from the plumbed ErrorInfo.
        retry_payload = {
            "category": category,
            "attempt": backoff,
            "backoff_seconds": result.delay,
        }
        if hinted:
            retry_payload["hinted"] = True
        _emit(log, "agent_retry", retry_payload, agent_id=None)
        await _interruptible_sleep(result.delay, coordinator)
        if hinted:
            hint_exempt_sleeps += 1
        else:
            cumulative_backoff += result.delay
        if coordinator.should_pause:
            return Pause(), None, {}

    if result.paused:
        if result.awaiting_user:
            # Headless bare-turn-end yield: mark THIS step resumable so the
            # reload keeps the transcript (the agent's last text is what the
            # AWAITING_USER resume delivers a reply to). Mirrors the resume:
            # edge's marker; cleared on the next forward Continue.
            state.resume_pending = state.step
            state.save(change.state_dir)
        return Pause(interrupt=result.interrupt), None, {}
    reincarnation_key = f"step:{step.phase}/{step.name}"
    if result.handoff_path is not None:
        # Tier A proactive handoff: the agent approached its window and (normally)
        # wrote a handoff note before stopping. Reincarnate the step fresh seeded
        # from the handoff (primary) plus the stashed transcript, up to the
        # per-session cap (shared with Tier B); at the cap, pause to the human.
        # Same cap counter as Tier B — both are reincarnations. The runner ALWAYS
        # reports handoff_path; the engine is the sole arbiter of handoff success
        # via note existence. A missing note means the handoff failed: degrade to a
        # transcript-only (Tier-B-shaped) reincarnation and log a DISTINCT reason so
        # a failed proactive handoff is never mistaken for a clean one.
        if (state.reincarnation_count(reincarnation_key)
                < state.max_reincarnations):
            state.bump_reincarnation(reincarnation_key)
            stashed = stash_transcript(transcript_dir)
            hp = Path(result.handoff_path)
            note_exists = hp.exists()
            reason = ("context threshold crossed" if note_exists
                      else "handoff failed — recovering from transcript")
            _emit(log, "reincarnating", {"reason": reason}, agent_id=None)
            seed = reincarnation_seed(
                stashed_dir=stashed,
                handoff_path=hp if note_exists else None)
            return await _route_agent(
                step, workflow, change, state, log,
                user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
                wrapper_text=wrapper_text, is_resume=False,
                test_gates=test_gates, headless=headless,
                autonomous=autonomous,
                sandbox_policy=sandbox_policy, seed_opening=seed,
                project_facts=project_facts, guidance=guidance)
        state.step_attempt = 0
        pause = Pause(reason="context_window", kind="reincarnation_cap")
        _emit(log, "step_completed",
              {"step": step.name, "kind": "agent",
               "action": _action_payload(pause), "var_mutations": {}})
        return pause, None, {}
    if (result.fatal and result.error is not None
            and result.error.category == "context_window"
            and state.reincarnation_count(reincarnation_key)
            < state.max_reincarnations):
        # Tier B backstop: recover from the hard window error by reincarnating
        # the step fresh — stash the transcript, seed the reborn session from it,
        # and re-enter — instead of pausing, up to the per-session cap.
        state.bump_reincarnation(reincarnation_key)
        stashed = stash_transcript(transcript_dir)
        _emit(log, "reincarnating",
              {"reason": "context window exceeded"}, agent_id=None)
        seed = reincarnation_seed(stashed_dir=stashed, handoff_path=None)
        return await _route_agent(
            step, workflow, change, state, log,
            user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
            wrapper_text=wrapper_text, is_resume=False,
            test_gates=test_gates, headless=headless,
            autonomous=autonomous,
            sandbox_policy=sandbox_policy, seed_opening=seed,
            project_facts=project_facts, guidance=guidance)
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
                                  is_resume=is_resume, test_gates=test_gates,
                                  headless=headless,
                                  autonomous=autonomous,
                                  sandbox_policy=sandbox_policy,
                                  seed_opening=seed_opening,
                                  project_facts=project_facts, guidance=guidance)

    state.step_attempt = 0
    _record_outcome(state, step.name, result.output, _worktree_head(change))
    bounce = _enforce_clean_tree(step, change, state, log)
    if bounce is not None:
        return bounce
    match = evaluate_rules(step.rules, vars=state.vars, output=result.output)
    state.vars.update(match.var_mutations)
    state.current_step = None
    state.save(change.state_dir)
    action, _, muts = _finish(log, step, change, match, state)
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
    test_gates: TestGates | None = None,
    headless: bool = False,
    interactive: bool = False,
    autonomous: bool = False,
    sandbox_policy: Any = None,
    project_facts: dict[str, str] | None = None,
    guidance: str = "",
    opening: str = "",
    feedback_lead: str | None = None,
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
    rendered_persona = ""
    if stored is not None:
        rendered = stored
        system_prompt_override: str | None = stored
    else:
        ctx = build_context(
            change_id=change.id, change_title=change.title,
            change_home=f".imp/changes/{change.id}",
            cur_base_head=change.cur_base_head,
            orig_base_head=change.orig_base_head,
            event_log=log, vars=state.vars,
            project_facts=project_facts,
            autonomous=autonomous, opening=opening,
            prior_laps=state.outcome_history.get(step_name, []),
        )
        rendered = render_prompt(
            agent_def.prompt_template_source, ctx,
            workflow_dir=change.workflow_dir,
        )
        rendered_persona = render_prompt(
            workflow.persona_source, ctx, workflow_dir=change.workflow_dir,
        ) if workflow.persona_source else ""
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
        rendered_persona=rendered_persona,
        system_prompt_override=system_prompt_override,
        initial_messages=initial_messages,
        change_context=ChangeContext(
            id=change.id, title=change.title,
            change_home=f".imp/changes/{change.id}",
            project_root=change.project_root, workflow_dir=change.workflow_dir,
            project_facts=project_facts or {}, guidance=guidance,
            persona_source=workflow.persona_source,
        ),
        user_chat_input_fn=user_chat_input_fn,
        transcript_dir=transcript_dir,
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
        tier=tier,
        subagent_depth=0,
        retry_feedback=retry_feedback,
        feedback_lead=feedback_lead,
        balance_on_resume=balance_on_resume,
        resume_should_continue=(
            (is_resume and state.interrupt_pending)
            or state.retry_continue_pending),
        log=log,
        test_gates=test_gates,
        headless=headless,
        interactive=interactive,
        sandbox_policy=sandbox_policy,
        checkpoint_threshold=state.checkpoint_threshold,
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
    """Resolve the SandboxPolicy for the current spawn point (IMP_* are
    change-stable, but project_root can flip on go-live, triggering a rebuild).
    Emit a durable posture event; on a hard-fail posture, emit + re-raise so the
    run stops rather than silently running bare."""
    cfg = sandbox_config_from_raw(config.sandbox or {})
    eng = overlay_engine_env(
        os.environ,
        engine_env(project_root=change.project_root, change_id=change.id,
                   change_title=change.title,
                   change_dir=change.change_dir,
                   test_gates=TestGates.from_config(config)),
    )
    try:
        policy = resolve_policy(
            worktree=change.project_root, control_root=change.control_root,
            change_id=change.id, cfg=cfg, engine_env_result=eng,
            environ=os.environ, available=sandbox_available(),
            change_dir=change.change_dir,
        )
    except SandboxUnavailable as exc:
        _emit(log, "sandbox_unavailable", {"message": str(exc)}, agent_id=None)
        raise
    if not policy.active:
        _emit(log, "sandbox_inactive", {"reason": policy.note}, agent_id=None)
    return policy
