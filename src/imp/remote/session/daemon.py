"""Per-quest session-daemon: bind run_quest behind a SessionServer."""
from __future__ import annotations

import argparse
import asyncio
import functools
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anyio

from imp.agents.compaction import compact_transcript
from imp.agents.skills import load_user_skill
from imp.cli.mutation_proxy import build_session_handler
from imp.diagnostics import install_stack_dumper
from imp.engine.abandon import (
    archive_conversation,
    discard_conversation,
    save_as_draft,
    tear_down_quest,
)
from imp.engine.completion import steer_hint
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.executor import RunResult, run_quest
from imp.engine.merge_queue import ensure_head_driven
from imp.engine.model_override import apply_model_override
from imp.engine.quest import Quest
from imp.engine.state import (
    EventLog,
    JsonlEventLog,
    LockHeld,
    ProjectLock,
    State,
    lock_is_live,
)
from imp.remote.session.attach import LAYOUT_MISMATCH_EXIT
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import SIDE_EFFECT_REASONS
from imp.remote.session.server import _DEFAULT_FIRST_ATTACH_TIMEOUT, SessionServer
from imp.remote.session.warmup import warm_module_graph

RunFn = Callable[..., Awaitable[Any]]

_RESUME_PROMPT = steer_hint("paused")
_RESUME_INTERRUPTED_PROMPT = steer_hint("interrupted")
_SCRIPT_HOLD_PROMPT = "paused · enter to re-run the gate · Ctrl-D to leave"
_SCRIPT_REDIRECT_REFUSED = (
    "a verify gate takes no redirect; press enter to re-run or Ctrl-D to leave")


def _is_interrupt_pause(result: Any, coordinator: Any) -> bool:
    return result is RunResult.PAUSED and coordinator.interrupted


def _is_side_effect_pause(result: Any, coordinator: Any) -> bool:
    return (result is RunResult.PAUSED and coordinator.should_pause
            and coordinator.pause_reason in SIDE_EFFECT_REASONS)


def _run_compaction(quest: Quest, on_event: Callable[[dict[str, Any]], None]) -> None:
    """Shed the parked step's tool-result bodies and emit the scrollback confirmation."""
    state = State.load(quest.state_dir)
    assert state.current_step is not None
    n = compact_transcript(state.current_step.transcript_dir)
    on_event({"type": "script_output",
              "payload": {"stdout": f"compacted — omitted {n} tool results"}})


def _trailing_is_workflow_resumed(log: EventLog) -> bool:
    entries = log.tail(2)
    return bool(entries) and entries[-1].get("type") == "workflow_resumed"


def _append_workflow_resumed(log: EventLog) -> None:
    if not _trailing_is_workflow_resumed(log):
        log.append(agent_id=None, type="workflow_resumed", payload={})


def _last_pause_was_awaiting_turn(log: EventLog) -> bool:
    for event in reversed(list(log.read())):
        etype = event.get("type")
        if etype == "workflow_paused":
            return (event.get("payload") or {}).get("reason") == "awaiting_turn"
        if etype == "workflow_resumed":
            return False
    return False


def _interrupt_gate_already_emitted(log: EventLog) -> bool:
    """True when the run is parked at a still-open inline interrupt steer gate."""
    for event in reversed(list(log.read())):
        if event.get("type") == "workflow_paused":
            continue
        return (event.get("type") == "awaiting_user_input"
                and bool((event.get("payload") or {}).get("interrupt")))
    return False


def _hold_prompt(
    result: Any, coordinator: Any, *, is_script: bool = False,
    log: EventLog | None = None,
) -> str | None:
    if (_is_interrupt_pause(result, coordinator)
            and coordinator.pause_reason not in SIDE_EFFECT_REASONS):
        if is_script:
            return _SCRIPT_HOLD_PROMPT
        if log is not None and _interrupt_gate_already_emitted(log):
            return None
        return _RESUME_INTERRUPTED_PROMPT
    return None


def _resolve_step_kind(quest: Quest) -> str | None:
    """The kind ('agent'/'script'/…) of the quest's current step, or None."""
    state = State.load(quest.state_dir)
    if state.step is None:
        return None
    from imp.engine.loader import load_workflow
    workflow = load_workflow(quest.workflow_dir)
    return next((s.kind for s in workflow.flat_steps()
                 if s.name == state.step), None)


async def serve_quest_session(
    quest: Quest,
    *,
    run_fn: RunFn = run_quest,
    hibernate: bool = True,
    tear_down: Callable[[Quest], None] = tear_down_quest,
    archive_left: Callable[[Quest], None] = archive_conversation,
    save_left: Callable[[Quest, str | None], None] = save_as_draft,
    discard_left: Callable[[Quest], None] = discard_conversation,
    resume: bool = False,
    non_interactive: bool = False,
    autonomous: bool = False,
    first_attach_timeout: float = _DEFAULT_FIRST_ATTACH_TIMEOUT,
    model: str | None = None,
    opening_unsandboxed: bool = False,
) -> Any:
    # Shared coordinator: socket `interrupt` control frame → run_quest's pause check.
    from imp.cli.signals import InterruptCoordinator

    coordinator = InterruptCoordinator()
    socket_path = str(session_socket_path(quest.id))
    attach_log = quest.event_log()

    def _on_first_attach() -> None:
        attach_log.append(agent_id=None, type="cli_attached", payload={})

    server = SessionServer(
        socket_path,
        coordinator=coordinator,
        lock=None,
        hibernate=hibernate,
        events_path=quest.events_path,
        on_first_attach=_on_first_attach,
        first_attach_timeout=first_attach_timeout,
        skill_loader=lambda name: load_user_skill(
            quest.workflow_dir / "skills", name),
        on_query=build_session_handler(quest.control_root, parent_id=quest.id),
    )
    # Raw broadcast, not the liveness fan-out: interrupt records must not pulse
    # note_activity().
    coordinator.set_event_log(
        JsonlEventLog(quest.events_path, on_event=server._emit))

    async def _bound(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        from imp.engine.awaiting_user_input import AwaitingUserInput

        def _paused_is_script() -> bool:
            return _resolve_step_kind(quest) == "script"

        resume_target: str | None = None
        if resume:
            log = quest.event_log()
            state = State.load(quest.state_dir)
            _append_workflow_resumed(log)
            # Crash recovery keys on the lock, not the status field. `crashed` is
            # snapshotted BEFORE acquire() (see below): a stale lock means the
            # prior run died mid-step, so replay the interrupted step instead of
            # re-burning it. A clean pause (no lock) leaves resume_pending unset.
            if crashed:
                state.arm_step_resume(should_continue=True)
            state.save(quest.state_dir)
            coordinator.set_unsandboxed(state.unsandboxed)
            # A crash replays the interrupted agent step, so it always lands in
            # the agent regardless of the last-stamped target.
            resume_target = None if crashed else state.resume_target
        run_kwargs: dict[str, Any] = {}
        if non_interactive:
            run_kwargs["headless"] = True
        if autonomous:
            run_kwargs["autonomous"] = True
        if model is not None:
            run_kwargs["opening_model"] = model
        if opening_unsandboxed:
            run_kwargs["opening_unsandboxed"] = True
        if (resume_target in ("prompt-interrupted", "prompt-paused")
                and not (non_interactive or autonomous)):
            # Resume back to the resteer hold prompt the user left from, before
            # any agent run: a typed reply then interjects and runs the agent;
            # a detach re-leaves it paused, re-stamping the same tell.
            # Only an interactive resume can answer the prompt; a headless or
            # autonomous resume has no channel to type a redirect, so honoring it
            # would re-pause without progress. Fall through to run the agent — it
            # replays the interrupted step (§6.15 mid-step ctrl-c resume).
            result: Any = RunResult.PAUSED
            prompt: str | None
            if _paused_is_script():
                prompt = _SCRIPT_HOLD_PROMPT
            elif resume_target == "prompt-interrupted":
                prompt = _RESUME_INTERRUPTED_PROMPT
            else:
                prompt = _RESUME_PROMPT
        else:
            result = await run_fn(
                quest,
                on_event=on_event,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
                wait_for_turn_fn=server.wait_for_turn,
                **run_kwargs,
            )
            coordinator.note_yield()  # run_quest returned -> the run has yielded
            prompt = _hold_prompt(
                result, coordinator, is_script=_paused_is_script(),
                log=quest.event_log())
        while (prompt is not None
               or _is_side_effect_pause(result, coordinator)):
            if _is_side_effect_pause(result, coordinator):
                if coordinator.pause_reason == "model-override":
                    # /model mid-conversation: executor parked+saved the step. Apply the
                    # override AFTER that save (no write race), then re-enter the same step.
                    arg = coordinator.take_pending_model()
                    if arg:
                        state = State.load(quest.state_dir)
                        assert state.step is not None
                        apply_model_override(quest, state, arg, step_name=state.step)
                elif coordinator.pause_reason == "compact":
                    _run_compaction(quest, on_event)
                coordinator.reset()
            else:
                on_event({"type": "awaiting_user_input",
                          "payload": {"steer_hint": prompt, "interrupt": True}})
                try:
                    reply = await user_chat_input_fn("")
                except AwaitingUserInput:
                    # Left at the hold loop. A ctrl-d mid-run interrupt detaches here
                    # with the agent mid-step: the cancellation unwound past the
                    # executor's Pause handler, so arm resume here exactly as it would
                    # have — native-resume the interrupted step (not re-seed) and take
                    # the continuation nudge on a no-message resume. Any other detach
                    # left the user at the resteer prompt: its markers were already
                    # armed by the executor Pause handler, so record the target only.
                    state = State.load(quest.state_dir)
                    if coordinator.pause_reason == "ctrl-d":
                        state.resume_target = "agent"
                        state.arm_step_resume(should_continue=True)
                    elif state.resume_should_continue:
                        # The executor armed the interrupt disposition (mid-turn
                        # Ctrl-C steer, Pause(interrupt=True)): preserve its
                        # continue-resume arming (resume_should_continue=True) and
                        # reproduce `interrupted ·` on resume. Re-arming a
                        # no-continue resume here would drop both.
                        state.resume_target = "prompt-interrupted"
                    else:
                        state.resume_target = "prompt-paused"
                        state.arm_step_resume(should_continue=False)
                    state.save(quest.state_dir)
                    return result
                coordinator.reset()
                if reply.strip():
                    if _paused_is_script():
                        on_event({"type": "script_output",
                                  "payload": {"stdout": _SCRIPT_REDIRECT_REFUSED}})
                        continue
                    coordinator.enqueue_interject(reply)
            _append_workflow_resumed(quest.event_log())
            result = await run_fn(
                quest,
                on_event=on_event,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
                wait_for_turn_fn=server.wait_for_turn,
                **run_kwargs,
            )
            coordinator.note_yield()  # run_quest returned -> the run has yielded
            prompt = _hold_prompt(
                result, coordinator, is_script=_paused_is_script(),
                log=quest.event_log())
        return result

    # Snapshot crash liveness BEFORE acquire(): ProjectLock.acquire() overwrites
    # the lock file with THIS daemon's own pid, so a probe read inside the lock
    # would always see a live pid and never detect the crash. `_bound` (defined
    # above) closes over `crashed`; it is read only when _bound runs, after this
    # assignment, so the forward reference is sound.
    lock_path = quest.quest_dir / "lock"
    crashed = resume and lock_path.exists() and not lock_is_live(lock_path)
    try:
        with ProjectLock(quest.quest_dir / "lock").acquire():
            await server.open()
            result = await server.serve(_bound)
    except LockHeld:
        # A wake-spawn raced a resident quest: exit harmlessly without binding socket.
        return None
    if server.abandoned:
        await anyio.to_thread.run_sync(tear_down, quest)
    elif server.saved:
        await anyio.to_thread.run_sync(save_left, quest, server.save_reason)
    elif server.archived:
        await anyio.to_thread.run_sync(archive_left, quest)
    elif server.discarded:
        await anyio.to_thread.run_sync(discard_left, quest)
    elif result is RunResult.PAUSED and _last_pause_was_awaiting_turn(quest.event_log()):
        await anyio.to_thread.run_sync(
            ensure_head_driven, quest.control_root, quest.base_branch)
    return result


def _iter_leaves(exc: BaseException) -> Iterator[BaseException]:
    """Yield every non-group leaf of exc, recursing through nested groups."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_leaves(sub)
    else:
        yield exc


def _config_pause_error(exc: BaseException) -> LoadError | LoadErrors | None:
    """Return the load error for a clean config pause, or None if any genuine
    (non-cancellation) crash leaf is present."""
    found: LoadError | LoadErrors | None = None
    for leaf in _iter_leaves(exc):
        if isinstance(leaf, (LoadError, LoadErrors)):
            if found is None:
                found = leaf
        elif not isinstance(leaf, asyncio.CancelledError):
            return None
    return found


def _record_load_failure(quest: Quest, exc: LoadError | LoadErrors) -> int:
    """Record a residual load failure as a config-classified error pause; exit 2."""
    message = str(exc)
    print(f"imp: {message}", file=sys.stderr)
    log = quest.event_log()
    log.append(
        agent_id=None, type="error",
        payload={"category": "config", "kind": type(exc).__name__,
                 "message": message, "detail": {}})
    log.append(
        agent_id=None, type="workflow_paused",
        payload={"reason": "config", "kind": type(exc).__name__})
    state = State.load(quest.state_dir)
    state.status = "paused"
    state.save(quest.state_dir)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imp-session-daemon")
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--quest-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--autonomous", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--unsandboxed", action="store_true")
    ns = parser.parse_args(argv)
    warm_module_graph()
    try:
        quest = Quest.load(Path(ns.control_root), ns.quest_id)
    except LayoutMismatch as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return LAYOUT_MISMATCH_EXIT
    except LoadError as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    install_stack_dumper(quest.quest_dir / "stack-dump.txt")
    kwargs: dict[str, Any] = {}
    if ns.resume:
        kwargs["resume"] = True
    if ns.non_interactive:
        kwargs["non_interactive"] = True
    if ns.autonomous:
        kwargs["autonomous"] = True
    if ns.model is not None:
        kwargs["model"] = ns.model
    if ns.unsandboxed:
        kwargs["opening_unsandboxed"] = True
    try:
        if kwargs:
            result = anyio.run(functools.partial(serve_quest_session, quest, **kwargs))
        else:
            result = anyio.run(serve_quest_session, quest)
    except (LoadError, LoadErrors) as exc:
        return _record_load_failure(quest, exc)
    except BaseExceptionGroup as eg:
        load_exc = _config_pause_error(eg)
        if load_exc is None:
            raise
        return _record_load_failure(quest, load_exc)
    return 1 if result is RunResult.PAUSED else 0


if __name__ == "__main__":
    raise SystemExit(main())
