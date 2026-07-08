"""Per-change session-daemon: bind run_change behind a SessionServer."""
from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from imp.engine.abandon import (
    archive_conversation,
    discard_conversation,
    save_to_backlog,
    tear_down_change,
)
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.merge_queue import ensure_head_driven
from imp.engine.state import EventLog, LockHeld, ProjectLock, State, lock_is_live
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import _DEFAULT_FIRST_ATTACH_TIMEOUT, SessionServer

RunFn = Callable[..., Awaitable[Any]]

_RESUME_PROMPT = "paused — type a message to redirect, or detach to leave it paused"


def _is_interrupt_pause(result: Any, coordinator: Any) -> bool:
    return result is RunResult.PAUSED and coordinator.should_pause


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


def _hold_prompt(result: Any, coordinator: Any) -> str | None:
    if _is_interrupt_pause(result, coordinator):
        return _RESUME_PROMPT
    return None


async def serve_change_session(
    change: Change,
    *,
    run_fn: RunFn = run_change,
    hibernate: bool = True,
    tear_down: Callable[[Change], None] = tear_down_change,
    archive_left: Callable[[Change], None] = archive_conversation,
    save_left: Callable[[Change], None] = save_to_backlog,
    discard_left: Callable[[Change], None] = discard_conversation,
    resume: bool = False,
    non_interactive: bool = False,
    autonomous: bool = False,
    first_attach_timeout: float = _DEFAULT_FIRST_ATTACH_TIMEOUT,
) -> Any:
    # Shared coordinator: socket `interrupt` control frame → run_change's pause check.
    from imp.cli.signals import InterruptCoordinator

    coordinator = InterruptCoordinator()
    socket_path = str(session_socket_path(change.control_root, change.id))
    attach_log = change.event_log()

    def _on_first_attach() -> None:
        attach_log.append(agent_id=None, type="cli_attached", payload={})

    server = SessionServer(
        socket_path,
        coordinator=coordinator,
        lock=None,
        hibernate=hibernate,
        events_path=change.events_path,
        on_first_attach=_on_first_attach,
        first_attach_timeout=first_attach_timeout,
    )

    async def _bound(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        from imp.engine.awaiting_user_input import AwaitingUserInput

        resume_target: str | None = None
        if resume:
            log = change.event_log()
            state = State.load(change.state_dir)
            _append_workflow_resumed(log)
            # Crash recovery keys on the lock, not the status field. `crashed` is
            # snapshotted BEFORE acquire() (see below): a stale lock means the
            # prior run died mid-step, so replay the interrupted step instead of
            # re-burning it. A clean pause (no lock) leaves resume_pending unset.
            if crashed:
                state.resume_pending = state.step
            state.save(change.state_dir)
            # A crash replays the interrupted agent step, so it always lands in
            # the agent regardless of the last-stamped target.
            resume_target = None if crashed else state.resume_target
        run_kwargs: dict[str, Any] = {}
        if non_interactive:
            run_kwargs["headless"] = True
        if autonomous:
            run_kwargs["autonomous"] = True
        if resume_target == "prompt" and not (non_interactive or autonomous):
            # Resume back to the resteer hold prompt the user left from, before
            # any agent run: a typed reply then interjects and runs the agent;
            # a detach re-leaves it paused, re-stamping "prompt".
            # Only an interactive resume can answer the prompt; a headless or
            # autonomous resume has no channel to type a redirect, so honoring it
            # would re-pause without progress. Fall through to run the agent — it
            # replays the interrupted step (§6.15 mid-step ctrl-c resume).
            result: Any = RunResult.PAUSED
            prompt: str | None = _RESUME_PROMPT
        else:
            result = await run_fn(
                change,
                on_event=on_event,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
                wait_for_turn_fn=server.wait_for_turn,
                **run_kwargs,
            )
            coordinator.note_yield()  # run_change returned -> the run has yielded
            prompt = _hold_prompt(result, coordinator)
        while prompt is not None:
            on_event({"type": "awaiting_user_input",
                      "payload": {"prompt": prompt}})
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
                state = State.load(change.state_dir)
                if coordinator.pause_reason == "ctrl-d":
                    state.resume_target = "agent"
                    state.arm_step_resume(interrupt=True)
                else:
                    state.resume_target = "prompt"
                state.save(change.state_dir)
                return result
            coordinator.reset()
            if reply.strip():
                coordinator.enqueue_interject(reply)
            _append_workflow_resumed(change.event_log())
            result = await run_fn(
                change,
                on_event=on_event,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
                wait_for_turn_fn=server.wait_for_turn,
                **run_kwargs,
            )
            coordinator.note_yield()  # run_change returned -> the run has yielded
            prompt = _hold_prompt(result, coordinator)
        return result

    # Snapshot crash liveness BEFORE acquire(): ProjectLock.acquire() overwrites
    # the lock file with THIS daemon's own pid, so a probe read inside the lock
    # would always see a live pid and never detect the crash. `_bound` (defined
    # above) closes over `crashed`; it is read only when _bound runs, after this
    # assignment, so the forward reference is sound.
    lock_path = change.change_dir / "lock"
    crashed = resume and lock_path.exists() and not lock_is_live(lock_path)
    try:
        with ProjectLock(change.change_dir / "lock").acquire():
            await server.open()
            result = await server.serve(_bound)
    except LockHeld:
        # A wake-spawn raced a resident change: exit harmlessly without binding socket.
        return None
    if server.abandoned:
        if change.materialized:
            await anyio.to_thread.run_sync(tear_down, change)
        else:
            await anyio.to_thread.run_sync(archive_left, change)
    elif server.saved:
        await anyio.to_thread.run_sync(save_left, change)
    elif server.archived:
        await anyio.to_thread.run_sync(archive_left, change)
    elif server.discarded:
        await anyio.to_thread.run_sync(discard_left, change)
    elif result is RunResult.PAUSED and _last_pause_was_awaiting_turn(change.event_log()):
        await anyio.to_thread.run_sync(
            ensure_head_driven, change.control_root, change.base_branch)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imp-session-daemon")
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--autonomous", action="store_true")
    ns = parser.parse_args(argv)
    try:
        change = Change.load(Path(ns.control_root), ns.change_id)
    except LoadError as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    kwargs: dict[str, Any] = {}
    if ns.resume:
        kwargs["resume"] = True
    if ns.non_interactive:
        kwargs["non_interactive"] = True
    if ns.autonomous:
        kwargs["autonomous"] = True
    try:
        if kwargs:
            result = anyio.run(functools.partial(serve_change_session, change, **kwargs))
        else:
            result = anyio.run(serve_change_session, change)
    except (LoadError, LoadErrors) as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    return 1 if result is RunResult.PAUSED else 0


if __name__ == "__main__":
    raise SystemExit(main())
