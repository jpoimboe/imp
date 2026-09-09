# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Workflow executor (unified step model): a single flat cursor over the
workflow's steps. Each step is dispatched by kind (agent/script/branch),
its rules block is evaluated, and the resulting Action moves (or terminates)
the cursor.

The engine owns state.json + events.jsonl. The runner owns transcript.jsonl.
An agent step entered fresh (no resume marker) wipes its transcript dir on
entry; a step entered via a `resume:` edge keeps it and balances a trailing
complete_step. The whole steps/ tree is removed at quest completion.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from imp.agents.prompts import build_context, render_label, render_prompt
from imp.agents.runner import (
    Message,
    QuestContext,
    TextBlock,
)
from imp.agents.skills import overlay_global_skills, overlay_project_skills, overlay_shared_partials
from imp.engine import quest_refs, readfault
from imp.engine import test_lane as _test_lane
from imp.engine.agent_run import (
    AgentRunResult,
    build_agent_invocation,
    drive_invocation,
)
from imp.engine.backoff import (
    interruptible_sleep as _interruptible_sleep,
)
from imp.engine.env import ALL_DECIDED, TestGates, Undecided, engine_env
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
from imp.engine.model_override import current_provider_model
from imp.engine.quest import Quest
from imp.engine.reincarnation import (
    reincarnation_seed,
    stash_transcript,
    write_transcript_owner,
)
from imp.engine.sandbox import (
    SANDBOX_SPAWN_MAX_ATTEMPTS,
    SandboxOpenFailed,
    SandboxUnavailable,
    _force_rmtree,
    make_home_layer,
    overlay_mode,
    resolve_policy,
    sandbox_available,
    sandbox_config_from_raw,
)
from imp.engine.schema import validate_agent_output
from imp.engine.seed import seed_workflow_start
from imp.engine.shim import imp_shim_dir, outer_imp_mismatch, write_imp_shim
from imp.engine.state import (
    CurrentStep,
    EventLog,
    JsonlEventLog,
    LaneRestore,
    State,
    classify_pause_halt,
)
from imp.engine.types import (
    Action,
    Continue,
    Goto,
    OutputSchema,
    Pause,
    Retry,
    Step,
    Vars,
    Workflow,
)
from imp.providers.claude_token_refresh import login_expires_at, login_expiry_warning
from imp.providers.effective import load_effective_config, validate_project_memory
from imp.providers.memory import load_memory_snapshot
from imp.providers.resolve import provider_from_model, resolve_run
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import SIDE_EFFECT_REASONS

if TYPE_CHECKING:
    from imp.providers.config import Config

EventCallback = Callable[[dict[str, Any]], None]

OUTCOME_HISTORY_K = 4

# Quest homes (keyed by quest_dir) whose snapshot this PROCESS already captured
# — captured at most once per process to stay version-consistent with this loader.
_SNAPSHOT_CAPTURED: set[str] = set()


def _record_outcome(state: State, step_name: str, output: dict[str, Any]) -> None:
    outcome = output.get("outcome")
    assert isinstance(outcome, str), f"outcome must be str; got {outcome!r}"
    hist = state.outcome_history.setdefault(step_name, [])
    next_lap = hist[-1]["lap"] + 1 if hist else 1
    hist.append({"lap": next_lap, "outcome": outcome})
    del hist[:-OUTCOME_HISTORY_K]


def _worktree_head(quest: Quest) -> str:
    """Worktree HEAD sha, or "" when HEAD is absent/unborn (like quest._imp_commit)."""
    from imp.engine import worktree
    proc = worktree._git(quest.project_root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _checkout_branch(quest: Quest) -> str:
    """The control root's branch — meaningful only before the worktree exists."""
    if quest.materialized:
        return ""
    from imp.engine import worktree
    return worktree.checked_out_branch(quest.control_root) or "a detached HEAD"


def _render_prior_attempts(step_name: str, entries: list[dict[str, Any]]) -> str:
    lines = ["⟳ Prior attempts at this step:"]
    for e in entries:
        lines.append(f'   lap {e["lap"]}:  {step_name} — "{e["outcome"]}"')
    return "\n".join(lines)


class RunResult(Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    ERRORED = "errored"


def _latest_engine_system_prompt(log: EventLog, agent_id: str) -> str | None:
    """The verbatim wire system prompt from the LATEST engine_message/system_prompt
    for this agent_id, or None if none exists. 'Latest' matters: a goto re-entry
    renders afresh and emits a newer engine_message; resume must replay the newest,
    not an earlier row. The prompt was emitted as a single text block
    (providers/*.py fresh-entry), so joining the block texts recovers it byte-exact.
    Resume is not a hot path and the log is per-quest, so a linear scan is fine."""
    found: str | None = None
    for e in log.read():
        payload = e.get("payload") or {}
        if (e.get("type") == "engine_message"
                and e.get("agent_id") == agent_id
                and payload.get("subtype") == "system_prompt"):
            found = "".join(
                b.get("text", "") for b in (payload.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text")
    return found


def _resume_system_prompt(
    log: EventLog, agent_id: str, transcript_dir: Any,
) -> str | None:
    """Resume-replay source. Returns the stored prompt to replay verbatim, or
    None when the caller should re-render. Aborts on the invariant violation:
    a non-empty transcript with no stored engine_message/system_prompt means
    messages were rendered against a now-lost prompt — re-rendering would
    silently diverge."""
    from imp.agents.transcript import Transcript
    stored = _latest_engine_system_prompt(log, agent_id)
    if stored is not None:
        return stored
    if Transcript(transcript_dir / "transcript.jsonl").exists_and_nonempty():
        raise RuntimeError(
            f"resume invariant violated: step {agent_id!r} has a non-empty "
            "transcript but no stored engine_message system prompt; refusing to "
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



def _liveness_on_event(coordinator: Any, on_event: EventCallback,
                       state: Any, state_dir: Path) -> EventCallback:
    """Compose subtree-liveness AND step-label persistence onto the run's
    `on_event`. Every append to the shared event log — by the task-lead OR any
    descendant subagent, since they all write through this one instance — pulses
    `coordinator.note_activity()` (resets the B.2 inactivity watchdog) and, on a
    `step_label_updated`, writes the pushed label into `state.step_label` and saves
    it, so `imp list` / Telegram `/list` (which read the persisted
    `state.step_label`) reflect the live task position. Only agents granted
    `update_step_label` (the step-level agents, e.g. task-lead) emit that event.
    The original driver callback is preserved (live narrative unaffected)."""
    def _composed(e: Any) -> None:
        coordinator.note_activity()
        if e.get("type") == "step_label_updated":
            state.step_label = e["payload"].get("label")
            state.save(state_dir)
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


def _tracked_workflow_path(control_root: Path, live: Path) -> str | None:
    """The workflow source's control-root-relative POSIX path when that path is
    present in the control root's HEAD (following the imp/workflows symlink to
    its real target), else None — the signal to fall back to a plain copy for a
    non-git source (a bundled/static workflow no merge queue mutates). Gating on
    HEAD (not the index) matches exactly what _capture_workflow_tree archives, so
    a staged-but-not-committed path never passes the check and then fails the
    archive."""
    try:
        rel = live.resolve().relative_to(control_root.resolve())
    except (OSError, ValueError):
        return None
    posix = rel.as_posix()
    try:
        done = subprocess.run(
            ["git", "-C", str(control_root), "cat-file", "-e", f"HEAD:{posix}"],
            capture_output=True, check=False,
        )
    except OSError:
        return None
    return posix if done.returncode == 0 else None


def _capture_workflow_tree(control_root: Path, live: Path, dest: Path) -> None:
    """Populate dest with the workflow tree. When the source is a git-tracked
    path under control_root, extract it from the control root's HEAD — a
    committed tree cannot be torn by a concurrent merge and pins exactly to the
    merge queue's committed state. Otherwise copy the live tree verbatim."""
    tracked = _tracked_workflow_path(control_root, live)
    if tracked is None:
        shutil.copytree(live, dest)
        return
    archived = subprocess.run(
        ["git", "-C", str(control_root), "archive", "--format=tar",
         f"HEAD:{tracked}"],
        capture_output=True, check=True,
    )
    dest.mkdir(parents=True)
    subprocess.run(
        ["tar", "-x", "-f", "-", "-C", str(dest)],
        input=archived.stdout, check=True,
    )


def _refresh_workflow_snapshot(quest: Quest) -> bool:
    """Re-snapshot the live workflow into quest_dir/workflow at a run_quest
    entry. Returns True only when an EXISTING snapshot's content was replaced
    (a fresh quest's initial copy returns False); best-effort and crash-safe."""
    quest_dir = quest.quest_dir
    snapshot = quest_dir / "workflow"
    tmp = quest_dir / "workflow.tmp"
    old = quest_dir / "workflow.old"

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
        live = quest.control_workflow_dir
    except Exception:
        return False
    if not live.is_dir():
        return False

    # 3. Build the candidate exactly as the initial snapshot does — but from a
    #    git-consistent HEAD view when the source is tracked, so a concurrent
    #    merge cannot tear it or feed a newer-than-HEAD tree to this loader.
    _capture_workflow_tree(quest.control_root, live, tmp)
    overlay_global_skills(tmp / "skills")
    overlay_project_skills(quest.project_root, tmp / "skills" / "user")
    overlay_shared_partials(tmp / "partials")

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


def _backfill_var_defaults(state: State, decls: Vars) -> list[str]:
    """Seed any workflow-declared var whose key is absent from state.vars with its
    declared default. Vars are seeded from the workflow only at quest creation
    (quest.py); a var added to the workflow later is missing from an in-flight
    quest's persisted vars, so a template referencing it under StrictUndefined
    (`{{ vars.x }}`) raises "'dict object' has no attribute 'x'" on resume. An
    already-set var keeps its persisted value. Returns the names added (sorted;
    empty if none), so the caller persists + logs only on a real change."""
    added = [name for name in decls.names() if name not in state.vars]
    for name in added:
        state.vars[name] = decls.defaults()[name]
    return sorted(added)


def _wipe_step_scratch(quest: Quest) -> None:
    """Empty Quest.tmp_dir and recreate it 0o700; called only on a genuine step advance."""
    from imp.remote.session.attach import ensure_quest_tmp_dir
    shutil.rmtree(quest.tmp_dir, ignore_errors=True)
    ensure_quest_tmp_dir(quest)


async def run_quest(
    quest: Quest,
    *,
    on_event: EventCallback | None = None,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,  # cli.signals.InterruptCoordinator | None
    headless: bool = False,
    autonomous: bool = False,
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None = None,
    opening_model: str | None = None,
    opening_unsandboxed: bool = False,
) -> RunResult:
    on_event = on_event or (lambda _e: None)
    if wait_for_turn_fn is None:
        wait_for_turn_fn = _headless_wait_for_turn
    if coordinator is None:
        # Lazy import to avoid circular dep at module-load time.
        from imp.cli.signals import InterruptCoordinator
        coordinator = InterruptCoordinator()

    prev_cwd = os.getcwd()
    os.chdir(quest.control_root)  # spec §5.3: engine cwd = control root, so a
    # resume launched from inside a worktree never blocks `git worktree move`.
    # NOTE: os.chdir is process-global — this hard-assumes one run_quest per
    # process (the foreground CLI model). Do not parallelize runs in-process
    # without revisiting (all git calls already pass explicit control_root).
    # ExitStack, not `with`: the sink needs `log`, built deep inside the body it wraps.
    sinks = ExitStack()
    try:
        snapshot_key = str(quest.quest_dir)
        captured_this_entry = snapshot_key not in _SNAPSHOT_CAPTURED
        if captured_this_entry:
            workflow_refreshed = _refresh_workflow_snapshot(quest)
            _SNAPSHOT_CAPTURED.add(snapshot_key)
        else:
            workflow_refreshed = False
        config = load_effective_config(control_root=quest.control_root).config
        if quest.provider is not None:
            # Resolve and reconcile read this local; refresh can't reintroduce the un-pinned vendor.
            config = dataclasses.replace(config, providers=(quest.provider,))
        state = State.load(quest.state_dir)
        quest.relink_home()
        if state.halt_condition is not None:
            # Work is resuming: drop a stale gate/error halt so the run reads running.
            state.halt_condition = None
            state.save(quest.state_dir)
        # Freeze once per quest: resume reuses the memory the first run saw.
        if state.memory is None:
            memory = load_memory_snapshot(quest.project_root / "imp" / "memory")
            validate_project_memory(quest.project_root, memory)
            state.memory = memory
            state.save(quest.state_dir)
        workflow = load_workflow(quest.workflow_dir)
        log: EventLog = JsonlEventLog(
            quest.events_path,
            on_event=_liveness_on_event(coordinator, on_event, state, quest.state_dir))
        sinks.enter_context(readfault.event_sink(log))
        if workflow_refreshed:
            _emit(log, "workflow_refreshed", {}, agent_id=None)
        if captured_this_entry:
            for warning_message in workflow.load_warnings:
                _emit(log, "warning",
                      {"source": "workflow_load", "message": warning_message,
                       "detail": {}},
                      agent_id=None)
            if (message := outer_imp_mismatch(quest.control_root)) is not None:
                _emit(log, "warning",
                      {"source": "outer_imp", "message": message, "detail": {}},
                      agent_id=None)
            login_message = login_expiry_warning(
                int(time.time() * 1000), login_expires_at())
            if login_message is not None:
                _emit(log, "warning",
                      {"source": "claude_login_expiry", "message": login_message,
                       "detail": {}},
                      agent_id=None)
        # First-resume bootstrap for a deferred backlog draft: an unstarted,
        # never-seeded stub (step None, has_started False) had its cursor + vars
        # deferred at creation. Seed now, before backfill, so apply_var_overrides
        # runs in full and backfill finds nothing to add (no spurious
        # vars_backfilled). A completed quest has has_started=True, so this
        # sentinel skips it and the step-None completion check below stays
        # authoritative.
        if state.step is None and not state.has_started:
            seed_workflow_start(state, workflow, config, quest_vars=quest.vars)
            state.save(quest.state_dir)
        # Seed any workflow var introduced after this quest was created (its
        # default is missing from the persisted vars); otherwise a StrictUndefined
        # template reference to it crashes the render on resume.
        backfilled = _backfill_var_defaults(state, workflow.vars)
        if backfilled:
            state.save(quest.state_dir)
            _emit(log, "vars_backfilled", {"names": backfilled}, agent_id=None)
        sandbox_policy = _build_sandbox_policy(quest, config, log)
        policy_root = quest.project_root

        # Resolve tiers once per run and persist, so resume never re-snapshots the
        # environment (design "When it runs"). run_quest re-executes on every
        # resume, so guard on the persisted map.
        did_fresh_resolve = state.resolved_tiers is None
        if did_fresh_resolve:
            resolved_tiers, resolved_steps, details = resolve_run(workflow, config)
            state.resolved_tiers = resolved_tiers
            state.resolved_steps = resolved_steps
            state.max_reincarnations = config.context_max_reincarnations()
            state.save(quest.state_dir)
            _emit(log, "tiers_resolved",
                  {"tiers": details["tiers"], "steps": details["steps"]},
                  agent_id=None)
            if quest.seed_opening:
                log.append_user_message(
                    agent_id=None,
                    message=Message(role="user",
                                    content=(TextBlock(text=quest.seed_opening),)),
                    subtype="intent")
        elif workflow_refreshed:
            if _reconcile_resolved_run(state, workflow, config):
                state.save(quest.state_dir)

        flat = workflow.flat_steps()
        index_of = {s.name: i for i, s in enumerate(flat)}
        # Durable source of truth: the intent survives any resume via quest.json,
        # not the transient run parameter (which resume loops never re-pass).
        pending_seed = quest.seed_opening  # one-shot: consumed at the entry step's fresh render
        opening_step = next((s.name for s in flat if s.kind == "agent"), None)
        if (did_fresh_resolve and opening_model is not None
                and opening_step is not None):
            from imp.engine.model_override import apply_model_override
            apply_model_override(
                quest, state, opening_model, step_name=opening_step)

        if did_fresh_resolve and opening_unsandboxed:
            # Step-scoped override for the opening/scoping conversation; the
            # step-boundary clear below retires it at go-live. Broadcast the marker
            # via the raw on_event callback — NOT _emit(log, ...) — so it never
            # persists and replays stale on a post-go-live re-attach.
            coordinator.set_unsandboxed(True)
            state.unsandboxed = True
            state.save(quest.state_dir)
            on_event({"type": "sandbox_marker",
                      "payload": {"unsandboxed": True}})

        # A hold mark surviving to process entry with lane_retained unset means a prior
        # run crashed while holding; with lane_retained set it was a clean interrupt /
        # give-up (consumed below, lane kept). Don't evict when the merge already
        # succeeded (merge_commit set) or the run is parked at completion — those
        # finalize via the idempotent merge path / _complete.
        if (state.held_lane is not None
                and not state.completion_pending
                and not state.merge_commit):
            if state.lane_retained:
                # A clean interrupt / give-up retained this lane. Consume the flag
                # once and keep the lane. Persist the cleared flag BEFORE the
                # resumed step dispatches: were it in-memory only, a SIGKILL
                # mid-resumed-step would leave lane_retained=True on disk and the
                # next entry would wrongly retain a possibly-mutated tree.
                state.lane_retained = False
                state.save(quest.state_dir)
            else:
                # Fires at resume, an unknown interval after the crash, so a
                # fingerprint taken here would gate nothing — decline and clear.
                _evict_if_holding(quest, state, log, record_restore=False)
                state.save(quest.state_dir)

        # In-memory, never persisted: last step this run dispatched (a resume never wipes).
        last_dispatched: str | None = None
        # Captured at process entry (not is_resume): an in-process bounce can't refill.
        resumed_step = state.resume_pending
        while True:
            if state.completion_pending:
                # §3: re-entry after an interrupt parked at the completion
                # boundary. Finalize directly — no dispatch, no transcript wipe.
                state.completion_pending = False
                return _complete(quest, state, log)
            if state.step is None:
                return _complete(quest, state, log)            # genuine completion
            if index_of.get(state.step) is None:
                # The cursor names a step absent from the loaded workflow
                # (corruption, or a refresh that dropped this step). Never
                # silent-merge: pause with an error instead of calling _complete.
                state.status = "error"
                state.halt_condition = None
                state.save(quest.state_dir)
                _emit_paused_event(log, "unknown_step", step=state.step)
                return RunResult.PAUSED
            idx = index_of[state.step]
            step = flat[idx]
            is_resume = (state.resume_pending == state.step)

            # Deliver the opening intent to the entry agent step on EVERY fresh
            # render (first run or restart), never a downstream step. Scoping to
            # the structural first agent step stops a mid-workflow resume from
            # swallowing the one-shot seed.
            seed_for_step = (
                pending_seed
                if pending_seed is not None and step.name == opening_step
                else None)
            # Go-live mid-run flips quest.project_root to the freshly-forked
            # worktree; the once-built policy is then rooted at the stale control
            # root. Rebuild so the next step's gates and agents bind the worktree.
            if quest.project_root != policy_root:
                sandbox_policy = _build_sandbox_policy(quest, config, log)
                policy_root = quest.project_root
            # var_mutations was ALREADY applied to state.vars and logged inside the
            # _route_* body — run_quest receives it for visibility only and must NOT
            # re-apply it (double-apply trap). The third element is otherwise unused.
            # A step is about to begin executing: record the durable run fact the
            # first time any step dispatches, so resume can distinguish a
            # never-run seed draft from a conversation that must replay. Persisted
            # now (before dispatch) so even a crash mid-step leaves it set.
            if not state.has_started:
                state.has_started = True
                if state.started_at is None:
                    state.started_at = datetime.now(UTC).isoformat()
                state.save(quest.state_dir)
            if last_dispatched is not None and last_dispatched != step.name:
                # Genuine advance to a different step: start it with a clean TMPDIR.
                _wipe_step_scratch(quest)
                # ...and a fresh reincarnation budget: the cap counts crossings
                # within one continuous engagement, not across the whole quest.
                state.reset_reincarnation(f"step:{step.phase}/{step.name}")
            last_dispatched = step.name
            if (resumed_step is not None and step.name == resumed_step
                    and step.kind == "agent"):
                # Process-entry single-shot: nulled below; an in-process advance to
                # a different step refills by design, above.
                state.reset_reincarnation(f"step:{step.phase}/{step.name}")
                resumed_step = None
            action, feedback_text, _var_mutations = await _dispatch_step(
                step, workflow, quest, state, log, is_resume=is_resume,
                user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
                test_gates=TestGates.from_config(config),
                pythonpath=config.pythonpath or (),
                undecided=Undecided.from_config(config),
                headless=headless,
                autonomous=autonomous,
                sandbox_policy=sandbox_policy,
                seed_opening=seed_for_step,
                memory=state.memory or {},
                wait_for_turn_fn=wait_for_turn_fn,
                max_parallel_test_runs=config.max_parallel_test_runs)
            if seed_for_step is not None:
                pending_seed = None

            if isinstance(action, Retry):
                raise AssertionError("Retry must not escape _dispatch_step")

            # §3 case 1 — mid-flight Pause: pause BEFORE navigation (as today).
            if isinstance(action, Pause):
                # An interrupt-caused pause resumes in place and continues the step.
                # Consult the DURABLE coordinator.interrupted latch, not just the
                # racy should_pause (end_gesture_round clears should_pause the
                # instant the client processes the pause, but interrupted survives
                # the whole in-flight run) nor action.interrupt alone (False on the
                # force-kill escalation teardown). clear_pause() resets interrupted
                # before any resume, so this term only fires inside the run that was
                # actually interrupted — a bare-turn-end yield leaves it False.
                interrupt_caused = (
                    coordinator.should_pause or coordinator.interrupted
                    or action.interrupt)
                # A between-turns side-effect pause (compact / model-override)
                # resumes in place exactly like an interrupt — interrupt_caused
                # stays True so `retain` below keeps the held lane and the
                # transcript — but it is NOT an interrupt gesture, so its resume
                # disposition is a solicit (return to the prompt), never a
                # continue (the stray agent turn).
                side_effect_pause = (
                    coordinator.should_pause
                    and coordinator.pause_reason in SIDE_EFFECT_REASONS)
                # Retain the held lane on an interrupt or transient give-up: the
                # step resumes in place on an untouched tree.
                retain = (interrupt_caused
                          or action.reason in ("retry_ceiling", "reauth_needed"))
                if retain and state.held_lane is not None:
                    state.lane_retained = True
                    evicted = False
                else:
                    evicted = _evict_if_holding(quest, state, log)
                if not evicted and step.kind == "agent":
                    state.arm_step_resume(
                        should_continue=(
                            not side_effect_pause
                            and (interrupt_caused
                                 or action.reason
                                 in ("retry_ceiling", "reauth_needed"))))
                reason = action.reason or "interrupt"
                interrupt_reason = (coordinator.pause_reason
                                    if coordinator.should_pause else None)
                if reason == "merge_blocked":
                    state.status = "merge_blocked"
                elif classify_pause_halt(reason, interrupt_reason) == "error":
                    state.status = "error"
                else:
                    state.status = "paused"
                state.halt_condition = None
                state.unsandboxed = coordinator.unsandboxed
                state.save(quest.state_dir)
                _emit_paused_event(
                    log, reason, kind=action.kind,
                    interrupt_reason=interrupt_reason)
                return RunResult.PAUSED

            target_idx = None
            if isinstance(action, Continue):
                state.loop_counts.pop(step.name, None)          # forward advance resets
                nxt = idx + 1
                if nxt >= len(flat):
                    state.clear_step_resume()
                    return _complete_or_park(quest, state, log, coordinator)
                state.step = flat[nxt].name
                state.step_label = (
                    state.route_label
                    or render_label(flat[nxt].label, quest, log, state)
                )
                state.phase_label = (
                    render_label(flat[nxt].phase_label, quest, log, state)
                    if flat[nxt].phase_label is not None else None
                )
                state.clear_step_resume()
            elif isinstance(action, Goto):
                if action.release is not None and state.held_lane is not None:
                    _release_held_lane(
                        action.release, quest, state, log, step_name=step.name)
                target_idx = workflow.resolve_goto(action.target)
                if target_idx >= len(flat):                      # empty trailing phase
                    state.clear_step_resume()
                    return _complete_or_park(quest, state, log, coordinator)
                if target_idx <= idx:                            # backward OR self → loop-counted
                    if action.progress:                          # progress edge → reset
                        state.loop_counts.pop(step.name, None)
                    else:
                        n = state.loop_counts.get(step.name, 0) + 1
                        state.loop_counts[step.name] = n
                        if n > step.max_loops and not coordinator.should_pause:
                            _evict_if_holding(quest, state, log)
                            state.loop_counts.pop(step.name, None)   # reset-on-pause: resume fresh
                            state.status = "error"
                            state.halt_condition = None
                            state.save(quest.state_dir)
                            _emit_paused_event(log, "max_loops", step=step.name)
                            return RunResult.PAUSED
                else:
                    state.loop_counts.pop(step.name, None)       # forward goto resets source
                    for leaped in range(idx + 1, target_idx):    # reset every leaped-over step
                        state.loop_counts.pop(flat[leaped].name, None)
                state.step = flat[target_idx].name
                state.step_label = (
                    state.route_label
                    or render_label(flat[target_idx].label, quest, log, state)
                )
                state.phase_label = (
                    render_label(flat[target_idx].phase_label, quest, log, state)
                    if flat[target_idx].phase_label is not None else None
                )
                if action.resume:
                    # Single-shot reset: a resumed step must not inherit a stale
                    # retry count. Done HERE (the navigation point that arms the
                    # resume), not in _route_agent — is_resume stays True across
                    # internal retries, so resetting per entry would let a
                    # persistent error retry forever.
                    state.step_attempt = 0
                    state.arm_step_resume(should_continue=False)
                else:
                    state.clear_step_resume()
            else:
                raise AssertionError(f"unexpected action {action!r}")

            # state.step just moved to a DIFFERENT step: if the unsandboxed override
            # was live it was step-scoped, so retire it here. Same-step pause/resume
            # paths (Pause return, max_loops pause, action-less re-dispatch)
            # short-circuit before this point, so the override survives a resume of
            # the same step. The guard skips the whole hook when the override was
            # never set, so an ordinary sandboxed quest broadcasts no redundant
            # marker. Broadcast the transient marker via the raw on_event callback —
            # NOT the persisting _emit(log, ...) — so it never reaches events.jsonl.
            if state.unsandboxed or coordinator.unsandboxed:
                state.unsandboxed = False
                coordinator.set_unsandboxed(False)
                on_event({"type": "sandbox_marker",
                          "payload": {"unsandboxed": False}})

            # Stash wrapper feedback for the target step to consume via the
            # fresh-entry opening message: the goto's rework feedback and/or a
            # prior-attempts memory of the target step's own recent outcomes.
            if isinstance(action, Goto) and target_idx is not None and target_idx < len(flat):
                target_name = flat[target_idx].name
                preamble = None
                # Backward/self goto = a rework re-entry. A feedback-carrying resume normally
                # replays the transcript; a bare one needs the preamble to keep the backstop.
                if target_idx <= idx and not (action.resume and feedback_text):
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
                # This branch is only reached under an interrupt, so retain the
                # held lane (resume on the navigated-to step); evict only when no
                # lane is held.
                if state.held_lane is not None:
                    state.lane_retained = True
                else:
                    _evict_if_holding(quest, state, log)
                state.status = "paused"
                state.halt_condition = None
                state.unsandboxed = coordinator.unsandboxed
                state.save(quest.state_dir)
                _emit_paused_event(
                    log, "interrupt", interrupt_reason=coordinator.pause_reason)
                return RunResult.PAUSED
            state.halt_condition = None
            state.unsandboxed = coordinator.unsandboxed
            state.save(quest.state_dir)
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
            state.arm_step_resume(should_continue=False)
            state.status = "error"
            state.halt_condition = None
            state.unsandboxed = coordinator.unsandboxed
            state.save(quest.state_dir)
        except Exception:
            # Best-effort: if the emit/save fails (or log/state was never bound), re-raise
            # the original (from None) so daemon.stderr surfaces it, not a spurious
            # UnboundLocalError.
            raise exc from None
        print(tb, file=sys.stderr)
        return RunResult.PAUSED
    finally:
        sinks.close()
        # prev_cwd may have been removed (e.g. resume from inside a worktree this
        # run merged + tore down). Don't let the restore mask the result/exception.
        try:
            os.chdir(prev_cwd)
        except OSError:
            os.chdir(quest.control_root)


def _complete_or_park(
    quest: Quest, state: State, log: EventLog, coordinator: Any,
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
        state.halt_condition = None
        state.unsandboxed = coordinator.unsandboxed
        state.save(quest.state_dir)
        _emit_paused_event(log, "interrupt", interrupt_reason=coordinator.pause_reason)
        return RunResult.PAUSED
    return _complete(quest, state, log)


def _complete(quest: Quest, state: State, log: EventLog) -> RunResult:
    """Terminal handler. Two paths:
    - No merge happened: mark `done`, archive the terminal snapshot, reap the
      whole home. Crash-safe: status='done' is saved before archive/reap, so a
      crash mid-reap leaves re-entry clean — re-running re-enters _complete
      (step=None, status='done') and idempotently re-archives + re-reaps.
    - merge-to-base already merged and exited the worktree (state.merge_commit set,
      base ref durably advanced): finalize — mark completed, archive the final
      snapshot. Ordering is crash-safe: status is written + saved BEFORE archive,
      so a crash mid-finalize leaves the merge durable. Recovery: a crash AFTER
      status='merged' is already done; a crash in the window between
      _route_merge_and_exit_worktree recording merge_commit and this status write
      leaves status='running' — re-running re-enters the merge step, which
      short-circuits on state.merge_commit (idempotent, no second merge) and
      completes cleanly.

    _complete does no `git merge`: the authoritative merge and its worktree exit
    live in the routable merge-to-base step (B3/F1), and the H1 dirty net moved
    into merge_to_base under the lock (R5). The no-merge branch DOES do the
    keep-branch worktree teardown (_exit_worktree: remove the worktree, keep the
    imp/<id> branch) before reaping the home."""
    state.held_lane = None
    state.held_acquire_step = None
    state.step = None
    state.step_label = None
    state.phase_label = None
    state.route_label = None
    from imp.engine import merge_queue
    if not state.merge_commit:
        _exit_worktree(quest)
        merge_queue.pop_and_wake(quest.control_root, quest.id, quest.base_branch)
        state.status = "done"
        state.save(quest.state_dir)
        _emit(log, "workflow_completed", {})
        from imp.engine import archive
        archive.archive_quest(
            quest.control_root, quest.id,
            events_path=quest.events_path,
            quest_json_path=quest.quest_dir / "quest.json",
            state_json_path=quest.state_dir / "state.json",
        )
        _force_rmtree(quest.quest_dir)
        shutil.rmtree(quest.tmp_dir, ignore_errors=True)
        session_socket_path(quest.id).unlink(missing_ok=True)
        _force_rmtree(quest.var_tmp_dir)
        return RunResult.COMPLETED

    state.status = "merged"
    state.save(quest.state_dir)
    _emit(log, "quest_merged",
          {"base": quest.base_branch, "commit": state.merge_commit})
    merge_queue.pop_and_wake(quest.control_root, quest.id, quest.base_branch)
    from imp.engine import archive
    archive.archive_quest(
        quest.control_root, quest.id,
        events_path=quest.events_path,
        quest_json_path=quest.quest_dir / "quest.json",
        state_json_path=quest.state_dir / "state.json",
    )
    _force_rmtree(quest.quest_dir)
    shutil.rmtree(quest.tmp_dir, ignore_errors=True)
    session_socket_path(quest.id).unlink(missing_ok=True)
    _force_rmtree(quest.var_tmp_dir)
    return RunResult.COMPLETED


def _apply_readonly_overlay(policy: Any, step: Step, project_root: Path) -> Any:
    """Compose step.read_only into a dispatch-time policy copy; no-op if empty or None.
    Additive: the step's overlays are appended to any overlays already baked into
    the policy (e.g. the workflow-snapshot overlay from resolve_policy), so a step's
    read_only never drops the baked-in protection. Both are ro binds; order within
    the category is immaterial."""
    if not step.read_only or policy is None:
        return policy
    overlays = tuple(str((project_root / p).resolve()) for p in step.read_only)
    return dataclasses.replace(
        policy, readonly_overlays=policy.readonly_overlays + overlays)


def _attach_home_layer(
    policy: Any, step: Step, quest: Quest,
) -> tuple[Any, Callable[[], None]]:
    """Attach the writable-home layer to `policy` and return (policy, cleanup).
    Overlay-capable host → an overlay ROOT that is a SIBLING of transcript_dir
    (keyed only by step.phase/step.name, so it survives its per-reincarnation
    rmtree); each agent run's keeper namespace mints one layer under that root,
    and cleanup removes the whole root at step teardown.
    Otherwise → a plain rw bind of the real home (fallback); cleanup is a no-op.
    An inactive or home-less policy (e.g. a script gate) is returned unchanged."""
    if not policy.active or not policy.home:
        return policy, (lambda: None)
    base = quest.quest_dir / "steps" / step.phase
    layer, overlay_root, cleanup = make_home_layer(
        policy.home,
        root=base / f"{step.name}.overlay",
        userxattr=overlay_mode(quest.quest_dir, Path(policy.home)),
    )
    return dataclasses.replace(
        policy, home_layer=layer, overlay_root=overlay_root), cleanup


async def _dispatch_step(
    step: Step, workflow: Workflow, quest: Quest,
    state: State, log: EventLog, *, is_resume: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any,
    test_gates: TestGates | None = None,
    pythonpath: tuple[str, ...] = (),
    undecided: Undecided = ALL_DECIDED,
    headless: bool = False,
    autonomous: bool = False,
    sandbox_policy: Any = None,
    seed_opening: str | None = None,
    memory: dict[str, str] | None = None,
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None = None,
    max_parallel_test_runs: int = 1,
) -> tuple[Action, str | None, dict[str, object]]:
    """Returns (action, feedback_text, var_mutations)."""
    wrapper_text: str | None = None
    fb = state.retry_feedback
    if fb is not None and fb.get("target_step") == step.name:
        wrapper_text = fb.get("text")
        state.retry_feedback = None
        state.save(quest.state_dir)

    if step.release is not None:
        _release_lane(step, quest, state, log)
    if step.acquire is not None and step.acquire != "test-lane":
        return await _route_acquire(
            step, workflow, quest, state, log, wait_for_turn_fn)

    sandbox_policy = _apply_readonly_overlay(sandbox_policy, step, quest.project_root)

    if step.kind == "branch":
        return _route_branch(step, quest, state, log)
    if step.kind == "merge_and_exit_worktree":
        return await _route_merge_and_exit_worktree(step, quest, state, log)
    if step.kind == "script":
        return await _route_script(step, quest, state, log,
                                   test_gates=test_gates,
                                   pythonpath=pythonpath,
                                   sandbox_policy=sandbox_policy,
                                   max_parallel_test_runs=max_parallel_test_runs,
                                   coordinator=coordinator)
    sandbox_policy, _home_cleanup = _attach_home_layer(sandbox_policy, step, quest)
    try:
        return await _route_agent(
            step, workflow, quest, state, log,
            user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
            wrapper_text=wrapper_text, is_resume=is_resume, test_gates=test_gates,
            pythonpath=pythonpath,
            undecided=undecided,
            headless=headless,
            autonomous=autonomous,
            sandbox_policy=sandbox_policy, seed_opening=seed_opening,
            memory=memory)
    finally:
        _home_cleanup()


def _step_started_payload(
    step: Step, state: State,
    quest: Quest | None = None, log: EventLog | None = None,
) -> dict[str, Any]:
    payload = {"step": step.name, "kind": step.kind, "phase": step.phase,
               "show_label": step.show_label,
               "show_output": step.show_output,
               "round": state.loop_counts.get(step.name, 0) + 1,
               "label": state.route_label or render_label(step.label, quest, log, state)}
    rotate = step.rotate_phrase if step.rotate_phrase is not None else step.interactive
    if rotate:
        payload["rotate_phrase"] = True
    if step.kind == "script":
        payload["command"] = step.command
    if step.position_label:
        payload["position_label"] = True
    if step.phase_label is not None:
        payload["phase_label"] = render_label(step.phase_label, quest, log, state)
    if not step.phase_show_label:
        payload["phase_show_label"] = False
    # Consume-on-read: transfer route_label from the prior step's _finish, then clear it.
    if state.route_label is not None:
        payload["route_label"] = state.route_label
        state.route_label = None
    return payload


def _emit_started(
    log: EventLog, step: Step, state: State, quest: Quest, **extra: Any,
) -> dict[str, Any]:
    return _emit(log, "step_started",
                 {**_step_started_payload(step, state, quest, log), **extra},
                 agent_id=None)


def _harvest_artifacts(
    quest: Quest, step: Step, warn: Callable[[str], None] | None = None
) -> None:
    """Journal quest_dir/artifacts/** to the quest's artifact ref (no-op if none)."""
    from imp.engine import worktree
    art_root = quest.quest_dir / "artifacts"
    if not art_root.is_dir():
        return
    files = {
        str(p.relative_to(art_root)): p
        for p in sorted(art_root.rglob("*")) if p.is_file()
    }
    worktree.artifact_commit(
        quest.control_root, quest.id, files,
        message=f"artifact: {step.name}", warn=warn)


def _finish(
    log: EventLog, step: Step, quest: Quest, match: Any, state: State
) -> tuple[Action, str | None, dict[str, object]]:
    def _warn(msg: str) -> None:
        _emit(log, "warning",
              {"source": "artifact_harvest", "message": msg, "detail": {}})
    # except Exception, not BaseException: lets CancelledError / Ctrl-C propagate.
    try:
        _harvest_artifacts(quest, step, warn=_warn)
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
    step: Step, quest: Quest, state: State, log: EventLog
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step, state, quest)
    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, quest, match, state)            # branch goto carries NO feedback


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
    step: Step, quest: Quest, state: State, log: EventLog,
    *, test_gates: TestGates | None = None,
    pythonpath: tuple[str, ...] = (),
    sandbox_policy: Any = None,
    max_parallel_test_runs: int = 1,
    coordinator: Any = None,
) -> tuple[Action, str | None, dict[str, object]]:
    _emit_started(log, step, state, quest)
    command = step.command
    assert command is not None, "script step must have a command"
    if "{{" in command:
        command = render_prompt(command, build_context(
            quest_title=quest.title, event_log=log,
            quest_id=quest.id, quest_home=quest.agent_quest_home,
            cur_base_head=quest.cur_base_head,
            orig_base_head=quest.orig_base_head,
            base_branch=quest.base_branch,
            checkout_branch=_checkout_branch(quest), vars=state.vars,
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
        quest.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="imp-output-", suffix=".json",
                                   dir=quest.tmp_dir)
        os.close(fd)
        output_path = Path(tmp)
    slot_fd: int | None = None
    try:
        if step.acquire == "test-lane":
            slot_fd = _test_lane.try_acquire(quest.control_root, max_parallel_test_runs)
            if slot_fd is None:
                _emit(log, "waiting_for_test_lane", {"step": step.name})
                slot_fd = await anyio.to_thread.run_sync(
                    functools.partial(_test_lane.acquire_blocking,
                                      quest.control_root, max_parallel_test_runs))
                _emit(log, "test_lane_acquired", {"step": step.name})
        def _emit_spawn_retry(next_attempt: int, detail: str) -> None:
            # open() fires this on the anyio WORKER thread; from_thread hops it back
            # to the loop thread, the single terminal writer. A hop, not a buffer:
            # the breadcrumb's whole job is telling the user WHY the gate is stalling,
            # so it has to appear during the stall, not batched to the gate's end.
            anyio.from_thread.run_sync(
                _emit, log, "runner_warning", {
                    "message": ("sandbox could not start the step; retrying "
                                f"({next_attempt}/{SANDBOX_SPAWN_MAX_ATTEMPTS})"),
                    "step": step.name, "reason": "sandbox-spawn-failed",
                    "detail": detail})

        run_call = functools.partial(
            run_script, step, cwd=quest.workflow_dir,
            env_extra=engine_env(project_root=quest.project_root,
                                 quest_id=quest.id,
                                 quest_title=quest.title,
                                 quest_dir=quest.quest_dir,
                                 control_root=quest.control_root,
                                 output_path=output_path,
                                 test_gates=test_gates,
                                 pythonpath=pythonpath),
            policy=sandbox_policy, command=command, abort=abort,
            shim_dir=str(imp_shim_dir(quest.quest_dir)),
            on_retry=_emit_spawn_retry)
        out = await anyio.to_thread.run_sync(run_call)
        if output_path is not None:
            assert step.output_schema is not None
            try:
                out = {**out, **_read_script_output(output_path, step.output_schema)}
            except ScriptOutputError as e:
                _finish(log, step, quest,
                        RuleMatch(action=Pause(reason="script_blocked"),
                                  rule=None, var_mutations={}), state)
                _emit(log, "script_blocked", {
                    "step": step.name, "exit": out["exit"],
                    "stdout": out["stdout"], "stderr": str(e)})
                return Pause(reason="script_blocked"), str(e), {}
    except ScriptInterrupted:
        return Pause(interrupt=True), None, {}
    except SandboxOpenFailed:
        # The budget is exhausted and NO command was spawned, so this exit is
        # infrastructure, not a gate verdict. NEVER touch the step's rules (no
        # evaluate_rules, no script_blocked).
        return Pause(reason="sandbox_spawn_failed"), None, {}
    finally:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        if slot_fd is not None:
            _test_lane.release(slot_fd)
    if step.show_output and out["exit"] == 0:
        _emit(log, "script_output", {"step": step.name, "stdout": out["stdout"]})
    match = evaluate_rules(step.rules, vars=state.vars, output=out)
    state.vars.update(match.var_mutations)
    action, _, muts = _finish(log, step, quest, match, state)
    feedback = None
    if isinstance(action, Goto):
        # exit-0: forward configured feedback (or None); non-zero: synthesize failure diagnostics.
        if out["exit"] == 0:
            feedback = action.feedback
        else:
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
    """Default wait_for_turn used when run_quest runs without a session server
    (tests, or a non-daemon caller): proceed iff already head, else hibernate."""
    from imp.engine.awaiting_user_input import AwaitingUserInput
    if predicate():
        return
    raise AwaitingUserInput()


def _resolve_lane(value: str, quest: Quest) -> str:
    if value == "merge-lane":
        return quest.base_branch
    raise RuntimeError(f"unknown lane keyword {value!r}")


async def _route_acquire(
    step: Step, workflow: Workflow, quest: Quest, state: State, log: EventLog,
    wait_for_turn_fn: Callable[[Callable[[], bool]], Awaitable[None]] | None,
) -> tuple[Action, str | None, dict[str, object]]:
    """Enqueue in lane; proceed as head, marking the hold; hibernate if unattended non-head."""
    from imp.engine import merge_queue
    from imp.engine.awaiting_user_input import AwaitingUserInput
    if wait_for_turn_fn is None:
        raise RuntimeError("wait_for_turn_fn is required for acquire steps")
    assert step.acquire is not None
    lane = _resolve_lane(step.acquire, quest)
    _emit_started(log, step, state, quest)
    merge_queue.enqueue_if_absent(quest.control_root, quest.id, lane)

    def _is_head() -> bool:
        return merge_queue.is_head(quest.control_root, quest.id, lane)

    if not _is_head():
        ahead = merge_queue.position(quest.control_root, quest.id, lane)
        _emit(log, "lane_waiting", {"base": lane, "ahead": ahead})
        last_ahead = ahead

        def _poll_turn() -> bool:
            nonlocal last_ahead
            current = merge_queue.position(quest.control_root, quest.id, lane)
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
    state.save(quest.state_dir)
    _emit(log, "lane_acquired", {"base": lane, "step": step.name, "glyph": step.glyph})
    restore = state.lane_restore
    if restore is not None:
        state.lane_restore = None            # consumed exactly once, either way
        state.save(quest.state_dir)
        # The recorded step must still exist: neither fingerprinted ref covers
        # imp's own packaged workflow, so an upgrade during the pause can drop it.
        if (workflow.step_index(restore.step) is not None
                and _lane_fingerprint(quest)
                == (restore.base_head, restore.quest_head)):
            _emit(log, "lane_restored", {"base": lane, "step": restore.step})
            goto = Goto(target=restore.step)
            _emit(log, "step_completed", {
                "step": step.name, "kind": step.kind,
                "action": _action_payload(goto), "var_mutations": {}})
            return goto, None, {}
    match = evaluate_rules(step.rules, vars=state.vars, output=None)
    state.vars.update(match.var_mutations)
    return _finish(log, step, quest, match, state)


def _release_held_lane(
    lane_keyword: str, quest: Quest, state: State, log: EventLog, *, step_name: str,
) -> None:
    """Pop the held lane and clear the hold mark, waking the next head."""
    from imp.engine import merge_queue
    lane = _resolve_lane(lane_keyword, quest)
    merge_queue.pop_and_wake(quest.control_root, quest.id, lane)
    state.held_lane = None
    state.held_acquire_step = None
    state.save(quest.state_dir)
    _emit(log, "lane_released", {"base": lane, "step": step_name})


def _release_lane(step: Step, quest: Quest, state: State, log: EventLog) -> None:
    """Pop the lane before the step body, clearing the hold mark."""
    assert step.release is not None
    _release_held_lane(step.release, quest, state, log, step_name=step.name)


def _lane_fingerprint(quest: Quest) -> tuple[str, str] | None:
    """(base branch tip, quest worktree HEAD), or None when either is unresolvable."""
    from imp.engine import worktree
    try:
        base_head = worktree.head_of(quest.control_root, quest.base_branch)
    except RuntimeError:
        return None
    quest_head = _worktree_head(quest)
    if not quest_head:
        return None
    return base_head, quest_head


def _capture_lane_restore(quest: Quest, state: State) -> LaneRestore | None:
    """All-or-nothing restore point for the cursor about to be rewound."""
    if state.step is None or state.step == state.held_acquire_step:
        return None
    fingerprint = _lane_fingerprint(quest)
    if fingerprint is None:
        return None
    return LaneRestore(
        step=state.step, base_head=fingerprint[0], quest_head=fingerprint[1])


def _evict_if_holding(quest: Quest, state: State, log: EventLog, *,
                      record_restore: bool = True) -> bool:
    """Release a held lane on pause/crash and rewind the resume cursor to the acquiring step."""
    if state.held_lane is None:
        return False
    from imp.engine import merge_queue
    lane = state.held_lane
    acquire_step = state.held_acquire_step
    # Capture before the pop: a quest woken by it could otherwise advance the
    # base into the record. record_restore=False also clears a crash leftover.
    state.lane_restore = (
        _capture_lane_restore(quest, state) if record_restore else None)
    merge_queue.pop_and_wake(quest.control_root, quest.id, lane)
    state.held_lane = None
    state.held_acquire_step = None
    state.step = acquire_step
    state.clear_step_resume()
    _emit(log, "lane_evicted", {"base": lane, "rewind_to": acquire_step})
    return True


def _exit_worktree(quest: Quest) -> None:
    """Idempotent: remove_worktree(force=True) tolerates an already-absent worktree."""
    from imp.engine import worktree
    worktree.remove_worktree(quest.control_root, quest.id, force=True)
    if quest.project_root != quest.control_root:
        quest.set_project_root(quest.control_root)


def _merge_blocked_pause(
    log: EventLog, step: Step, quest: Quest, status: str, detail: str | None,
    *, note: str | None = None,
) -> tuple[Action, str | None, dict[str, object]]:
    reason = "worktree_dirty" if status == "DIRTY" else status.lower()
    payload: dict[str, Any] = {"reason": reason, "base": quest.base_branch, "detail": detail}
    if note is not None:
        payload["note"] = note
    _emit(log, "merge_blocked", payload)
    pause = Pause(reason="merge_blocked", kind=reason)
    _emit(log, "step_completed",
          {"step": step.name, "kind": "merge_and_exit_worktree",
           "action": _action_payload(pause), "var_mutations": {}})
    return pause, None, {}


async def _route_merge_and_exit_worktree(
    step: Step, quest: Quest, state: State, log: EventLog,
) -> tuple[Action, str | None, dict[str, object]]:
    from imp.engine import worktree
    _emit_started(log, step, state, quest)

    if state.merge_commit:
        _exit_worktree(quest)
        match = evaluate_rules(step.rules, vars=state.vars, output={"merge": "MERGED"})
        state.vars.update(match.var_mutations)
        return _finish(log, step, quest, match, state)

    spec_path = quest.quest_dir / "artifacts" / "spec" / f"{quest.id}.md"
    body = _decisions_body(spec_path)
    result = await anyio.to_thread.run_sync(
        functools.partial(
            worktree.merge_to_base,
            quest.control_root, quest.id, quest.base_branch, title=quest.title,
            body=body, project_root=quest.project_root))
    if result.status == "MERGED":
        state.merge_commit = result.merge_commit
        state.save(quest.state_dir)
        _exit_worktree(quest)

    match = evaluate_rules(step.rules, vars=state.vars, output={"merge": result.status})

    if isinstance(match.action, Goto):                # a recovery route
        attempt = state.loop_counts.get(step.name, 0)
        if attempt >= step.max_loops:                 # exhausted — pause one attempt early
            state.loop_counts.pop(step.name, None)    # reset-on-pause: resume re-enters fresh
            note = (f"imp attempted {step.max_loops} recovery passes and the "
                    "merge still failed.")
            return _merge_blocked_pause(
                log, step, quest, result.status, result.detail, note=note)
        _emit(log, "merge_retry",
              {"status": result.status, "attempt": attempt + 1,
               "max": step.max_loops, "base": quest.base_branch})
        state.vars.update(match.var_mutations)
        action, _, muts = _finish(log, step, quest, match, state)
        # _finish hardcodes its feedback element to None; thread the rule's own
        # feedback so a DIRTY route's preamble reaches the reconciler (mirrors the
        # agent route).
        return action, match.action.feedback, muts

    if isinstance(match.action, Pause):               # REFUSED or any other status
        return _merge_blocked_pause(log, step, quest, result.status, result.detail)

    state.vars.update(match.var_mutations)
    return _finish(log, step, quest, match, state)


def _enforce_clean_tree(
    step: Step, quest: Quest, state: State, log: EventLog,
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
            or quest.project_root == quest.control_root
            or not worktree.is_worktree(quest.project_root)):
        state.clean_tree_bounces.pop(key, None)
        return None
    paths = worktree.uncommitted_paths(quest.project_root)
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
    state.save(quest.state_dir)
    return action, format_clean_tree_feedback(paths), {}


async def _route_agent(
    step: Step, workflow: Workflow, quest: Quest,
    state: State, log: EventLog, *,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    coordinator: Any, wrapper_text: str | None, is_resume: bool,
    test_gates: TestGates | None = None,
    pythonpath: tuple[str, ...] = (),
    undecided: Undecided = ALL_DECIDED,
    headless: bool = False,  # forwarded to the invocation/child runs
    autonomous: bool = False,
    sandbox_policy: Any = None,
    seed_opening: str | None = None,
    reincarnation_text: str | None = None,
    memory: dict[str, str] | None = None,
) -> tuple[Action, str | None, dict[str, object]]:
    # step.file is guaranteed non-None: loader requires file:/prompt:, and
    # _v_agent_has_file rejects inline prompt: agents this milestone.
    assert step.file is not None, "agent step must have a file this milestone"
    agent_def = workflow.agents[step.file.stem]
    transcript_dir = quest.quest_dir / "steps" / step.phase / step.name

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
        write_transcript_owner(
            transcript_dir,
            current_provider_model(state, workflow, step.name).provider)
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
    state.save(quest.state_dir)

    backoff = 0
    cumulative_backoff = 0.0
    hint_exempt_sleeps = 0
    rotation_exempt_sleeps = 0
    while True:
        result = await _run_agent_invocation(
            agent_id=f"step:{step.phase}/{step.name}", agent_def=agent_def,
            tier=step.tier, step_name=step.name,
            workflow=workflow, quest=quest, state=state, log=log,
            transcript_dir=transcript_dir,
            step_started_payload_extras={
                **_step_started_payload(step, state, quest, log),
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
            test_gates=test_gates, pythonpath=pythonpath, undecided=undecided, headless=headless,
            interactive=step.interactive,
            autonomous=autonomous,
            sandbox_policy=sandbox_policy,
            memory=memory,
            opening=run_opening, feedback_lead=feedback_lead,
            reincarnation_text=reincarnation_text)
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
        # A peer rotating the OAuth token is not a failing endpoint — the retry
        # runs on the token now on disk — so hold the first races at the ladder's
        # bottom rung. Capped at 3 like the hinted exemption, so a rotator that
        # never settles falls back to the true rung and still reaches the budget.
        raced = (result.error is not None
                 and result.error.kind == "token_rotated"
                 and rotation_exempt_sleeps < 3)
        delay = 1.0 if raced else result.delay
        if not hinted:
            budget = budget_for(category)
            if budget is not None and cumulative_backoff + delay > budget:
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
        state.arm_step_resume(should_continue=True, cause="provider_retry")
        state.save(quest.state_dir)
        # Self-healed-retry visibility (agent_retry): surface the backoff so a
        # transient network/rate_limit error that recovers doesn't look like a
        # failure. category comes from the plumbed ErrorInfo.
        retry_payload = {
            "category": category,
            "attempt": backoff,
            "backoff_seconds": delay,
        }
        if result.error is not None and result.error.message:
            retry_payload["message"] = result.error.message
        if result.error is not None and result.error.kind:
            retry_payload["kind"] = result.error.kind
        if hinted:
            retry_payload["hinted"] = True
        _emit(log, "agent_retry", retry_payload, agent_id=None)
        coordinator.begin_backoff_wait()
        try:
            await _interruptible_sleep(delay, coordinator)
        finally:
            coordinator.end_backoff_wait()
        # Consume the retry-now latch so it doesn't leak into the next backoff sleep.
        resumed_early = coordinator.take_retry_now()
        _emit(log, "backoff_ended",
              {"reason": "retry_now" if resumed_early else "elapsed"}, agent_id=None)
        if hinted:
            hint_exempt_sleeps += 1
        else:
            cumulative_backoff += delay
        if raced:
            rotation_exempt_sleeps += 1
        if coordinator.should_pause:
            return Pause(), None, {}

    if result.paused:
        if result.awaiting_user:
            # Headless bare-turn-end yield: mark THIS step resumable so the
            # reload keeps the transcript (the agent's last text is what the
            # AWAITING_USER resume delivers a reply to). Mirrors the resume:
            # edge's marker; cleared on the next forward Continue.
            state.arm_step_resume(should_continue=result.interrupt)
            state.save(quest.state_dir)
        # A genuine your-turn yield gets its own reason so it stops sharing the
        # "interrupt" token; a real interrupt (interrupt=True) or a
        # subagent-suspend unwind (awaiting_user=False) keeps reason=None and
        # still defaults to "interrupt" at the emit site.
        return Pause(
            reason="awaiting_user" if (
                result.awaiting_user and not result.interrupt) else None,
            interrupt=result.interrupt,
        ), None, {}
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
            # The stash stays on disk for human forensics; nothing reads it back.
            stash_transcript(transcript_dir)
            hp = Path(result.handoff_path)
            handoff_text = hp.read_text() if hp.exists() else None
            reason = ("context threshold crossed" if handoff_text is not None
                      else "handoff failed — recovering from transcript")
            _emit(log, "reincarnating", {"reason": reason}, agent_id=None)
            seed = reincarnation_seed(
                handoff_text=handoff_text, quest_id=quest.id,
                step_name=step.name)
            return await _route_agent(
                step, workflow, quest, state, log,
                user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
                wrapper_text=wrapper_text, is_resume=False,
                test_gates=test_gates, pythonpath=pythonpath, undecided=undecided,
                headless=headless,
                autonomous=autonomous,
                sandbox_policy=sandbox_policy,
                seed_opening=quest.seed_opening, reincarnation_text=seed,
                memory=memory)
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
        stash_transcript(transcript_dir)
        _emit(log, "reincarnating",
              {"reason": "context window exceeded"}, agent_id=None)
        seed = reincarnation_seed(handoff_text=None, quest_id=quest.id,
                                  step_name=step.name)
        return await _route_agent(
            step, workflow, quest, state, log,
            user_chat_input_fn=user_chat_input_fn, coordinator=coordinator,
            wrapper_text=wrapper_text, is_resume=False,
            test_gates=test_gates, pythonpath=pythonpath, undecided=undecided, headless=headless,
            autonomous=autonomous,
            sandbox_policy=sandbox_policy,
            seed_opening=quest.seed_opening, reincarnation_text=seed,
            memory=memory)
    if result.fatal:
        # Defense in depth: reset the retry count when pausing fatally, matching
        # the success/exhaustion resets. Runs once (at the pause), unlike the
        # resume reset which is gated on the navigation point.
        state.step_attempt = 0
        fatal_category = result.error.category if result.error else None
        # auth_terminal is actionable: reauth_needed brings its copy, label and resume-in-place.
        pause = Pause(
            reason=("reauth_needed" if fatal_category == "auth_terminal" else fatal_category),
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
        return await _route_agent(step, workflow, quest, state, log,
                                  user_chat_input_fn=user_chat_input_fn,
                                  coordinator=coordinator, wrapper_text=wrapper_text,
                                  is_resume=is_resume, test_gates=test_gates,
                                  pythonpath=pythonpath,
                                  undecided=undecided,
                                  headless=headless,
                                  autonomous=autonomous,
                                  sandbox_policy=sandbox_policy,
                                  seed_opening=seed_opening,
                                  reincarnation_text=reincarnation_text,
                                  memory=memory)

    state.step_attempt = 0
    _record_outcome(state, step.name, result.output)
    bounce = _enforce_clean_tree(step, quest, state, log)
    if bounce is not None:
        return bounce
    match = evaluate_rules(step.rules, vars=state.vars, output=result.output)
    state.vars.update(match.var_mutations)
    state.current_step = None
    state.save(quest.state_dir)
    action, _, muts = _finish(log, step, quest, match, state)
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


def _reconcile_sandbox_policy(policy: Any, quest: Quest) -> Any:
    """Re-root a policy baked before go-live at the current quest.project_root.

    Materialization flips project_root mid-step; every re-entry (backoff retry,
    errored retry, both reincarnations) funnels through _run_agent_invocation,
    so reconciling here covers all of them. Materialization changes exactly the
    two fields create_and_enter_worktree patches on its own ToolContext.
    """
    if policy is None or policy.worktree == str(quest.project_root):
        return policy
    return dataclasses.replace(
        policy,
        worktree=str(quest.project_root),
        git_worktree_self=(
            str(quest.control_root / ".git" / "worktrees" / quest.project_root.name)
            if quest.project_root != quest.control_root else ""),
    )


async def _run_agent_invocation(
    *,
    agent_id: str,
    agent_def: Any,
    tier: str,
    step_name: str,
    workflow: Workflow,
    quest: Quest,
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
    pythonpath: tuple[str, ...] = (),
    undecided: Undecided = ALL_DECIDED,
    headless: bool = False,
    interactive: bool = False,
    autonomous: bool = False,
    sandbox_policy: Any = None,
    memory: dict[str, str] | None = None,
    opening: str = "",
    feedback_lead: str | None = None,
    reincarnation_text: str | None = None,
) -> AgentRunResult:
    """Run one top-level agent step to a terminal AgentRunResult.

    Computes the executor's caller-specific inputs (model resolution from the
    persisted tier map, prompt rendering against the shared write-log,
    subagent_defs + max_tool_calls resolution), emits step_started, then defers
    construction and the drain loop to the shared builder + driver. The backoff
    sleep loop lives in _route_agent (it owns agent_retry events + transcript
    re-entry). See drive_invocation for the field semantics of the result.
    """
    sandbox_policy = _reconcile_sandbox_policy(sandbox_policy, quest)
    assert state.resolved_tiers is not None  # set by run_quest before dispatch
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
        parent_ref, depends_refs = quest_refs.resolve_for_quest(quest)
        ctx = build_context(
            quest_id=quest.id, quest_title=quest.title,
            quest_home=quest.agent_quest_home,
            cur_base_head=quest.cur_base_head,
            orig_base_head=quest.orig_base_head,
            base_branch=quest.base_branch,
            checkout_branch=_checkout_branch(quest),
            event_log=log, vars=state.vars,
            autonomous=autonomous, opening=opening,
            prior_laps=state.outcome_history.get(step_name, []),
            parent=parent_ref, depends_on=depends_refs,
            undecided=undecided,
        )
        rendered = render_prompt(
            agent_def.prompt_template_source, ctx,
            workflow_dir=quest.workflow_dir,
        )
        rendered_persona = render_prompt(
            workflow.persona_source, ctx, workflow_dir=quest.workflow_dir,
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
        quest_context=QuestContext(
            id=quest.id, title=quest.title,
            quest_home=quest.agent_quest_home, quest_dir=quest.quest_dir,
            project_root=quest.project_root, workflow_dir=quest.workflow_dir,
            memory=memory or {},
            workflow_name=workflow.name, step_name=step_name,
            persona_source=workflow.persona_source,
        ),
        user_chat_input_fn=user_chat_input_fn,
        transcript_dir=transcript_dir,
        events_path=quest.events_path,
        subagent_defs=subagent_defs,
        tiers=tuple(state.resolved_tiers.keys()) if state.resolved_tiers else (),
        max_tool_calls=resolved_max_tool_calls,
        agent_id=agent_id,
        event_log=log,
        quest_state=state,
        quest=quest,
        first_event_parent_id=started["id"],
        resolved_tiers=state.resolved_tiers,
        coordinator=coordinator,
        tier=tier,
        subagent_depth=0,
        retry_feedback=retry_feedback,
        feedback_lead=feedback_lead,
        reincarnation_text=reincarnation_text,
        balance_on_resume=balance_on_resume,
        resume_should_continue=(
            state.resume_pending == state.step
            and state.resume_should_continue),
        resume_cause=state.resume_cause,
        log=log,
        test_gates=test_gates,
        pythonpath=pythonpath,
        undecided=undecided,
        headless=headless,
        interactive=interactive,
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


def _warn_if_no_overlay(policy: Any, quest: Quest, log: EventLog) -> None:
    """When the sandbox is active but the host lacks unprivileged overlayfs, $HOME
    falls back to a writable bind of the REAL home (home isolation lost; secret
    masks still apply). Emit one durable warning, modeled on sandbox_inactive."""
    if policy.active and policy.home and overlay_mode(
            quest.quest_dir, Path(policy.home)) is None:
        _emit(log, "sandbox_overlay_unavailable",
              {"reason": "sandbox: unprivileged overlayfs unavailable — $HOME is "
                         "a writable bind of your real home, so agent writes to "
                         "$HOME persist (secrets are still masked)"},
              agent_id=None)


def _build_sandbox_policy(quest: Quest, config: Any, log: EventLog) -> Any:
    """Resolve the SandboxPolicy for the current spawn point (IMP_* are
    quest-stable, but project_root can flip on go-live, triggering a rebuild).
    Emit a durable posture event; on a hard-fail posture, emit + re-raise so the
    run stops rather than silently running bare."""
    write_imp_shim(quest.quest_dir)
    cfg = sandbox_config_from_raw(config.sandbox or {})
    eng = engine_env(project_root=quest.project_root, quest_id=quest.id,
                     quest_title=quest.title,
                     quest_dir=quest.quest_dir,
                     control_root=quest.control_root,
                     test_gates=TestGates.from_config(config),
                     pythonpath=config.pythonpath or ())
    try:
        policy = resolve_policy(
            worktree=quest.project_root, control_root=quest.control_root,
            quest_id=quest.id, cfg=cfg, engine_env_result=eng,
            environ=os.environ, available=sandbox_available(),
            quest_dir=quest.quest_dir,
        )
    except SandboxUnavailable as exc:
        _emit(log, "sandbox_unavailable", {"message": str(exc)}, agent_id=None)
        raise
    if not policy.active:
        _emit(log, "sandbox_inactive", {"reason": policy.note}, agent_id=None)
    _warn_if_no_overlay(policy, quest, log)
    if policy.active:
        # TMPDIR points at Quest.tmp_dir; ensure it exists 0o700 for the sandboxed
        # agent + host provisioning scratch (spawn_session_daemon does this too — the
        # call is idempotent and covers direct-run paths with no daemon).
        from imp.remote.session.attach import ensure_quest_tmp_dir
        ensure_quest_tmp_dir(quest)
    if policy.active and policy.quest_var_tmp_dir:
        # The bwrap SOURCE for the namespace /var/tmp; --bind (not --bind-try)
        # requires it to exist before any sandboxed spawn. Chmod the imp-<uid> parent
        # to 0o700 too (mkdir -p leaves it at umask perms, and /var/tmp is a
        # world-writable sticky dir); the chmods also correct a pre-existing wrong mode.
        var_tmp_leaf = Path(policy.quest_var_tmp_dir)
        var_tmp_leaf.mkdir(parents=True, exist_ok=True, mode=0o700)
        var_tmp_leaf.chmod(0o700)
        var_tmp_leaf.parent.chmod(0o700)
    return policy
