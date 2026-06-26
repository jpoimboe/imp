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


def _last_pause_was_script_blocked(log: EventLog) -> bool:
    for event in reversed(list(log.read())):
        etype = event.get("type")
        if etype == "workflow_paused":
            return (event.get("payload") or {}).get("reason") == "script_blocked"
        if etype == "workflow_resumed":
            return False
    return False


def _last_script_blocked_stderr(log: EventLog) -> str:
    for event in reversed(list(log.read())):
        if event.get("type") == "script_blocked":
            return (event.get("payload") or {}).get("stderr") or ""
    return ""


def _hold_prompt(result: Any, coordinator: Any, log: EventLog) -> str | None:
    if _is_interrupt_pause(result, coordinator):
        return _RESUME_PROMPT
    if result is RunResult.PAUSED and _last_pause_was_script_blocked(log):
        stderr = _last_script_blocked_stderr(log).strip()
        return f"{stderr}\n{_RESUME_PROMPT}" if stderr else _RESUME_PROMPT
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
    seed_opening: str | None = None,
    resume: bool = False,
    opening_intent: str | None = None,
    scope_only: bool = False,
    non_interactive: bool = False,
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
        from imp.agents.tools.wait_for_user import AwaitingUserInput

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
        run_kwargs: dict[str, Any] = {}
        if scope_only:
            run_kwargs["scope_only"] = True
        if non_interactive:
            run_kwargs["non_interactive"] = True
        first_kwargs: dict[str, Any] = {}
        if seed_opening is not None:
            first_kwargs["seed_opening"] = seed_opening
        if opening_intent is not None:
            first_kwargs["opening_intent"] = opening_intent
        result = await run_fn(
            change,
            on_event=on_event,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
            wait_for_turn_fn=server.wait_for_turn,
            **run_kwargs,
            **first_kwargs,
        )
        prompt = _hold_prompt(result, coordinator, change.event_log())
        while prompt is not None:
            on_event({"type": "awaiting_user_input",
                      "payload": {"prompt": prompt}})
            try:
                reply = await user_chat_input_fn("")
            except AwaitingUserInput:
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
            prompt = _hold_prompt(result, coordinator, change.event_log())
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
    parser.add_argument("--seed-opening", default=None)
    parser.add_argument("--opening-intent", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--scope-only", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    ns = parser.parse_args(argv)
    try:
        change = Change.load(Path(ns.control_root), ns.change_id)
    except LoadError as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    kwargs: dict[str, Any] = {}
    if ns.seed_opening is not None:
        kwargs["seed_opening"] = ns.seed_opening
    if ns.opening_intent is not None:
        kwargs["opening_intent"] = ns.opening_intent
    if ns.resume:
        kwargs["resume"] = True
    if ns.scope_only:
        kwargs["scope_only"] = True
    if ns.non_interactive:
        kwargs["non_interactive"] = True
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
