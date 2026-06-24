"""Per-change session-daemon: bind run_change behind a SessionServer."""
from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from imp.engine.abandon import archive_conversation, save_to_backlog, tear_down_change
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.merge_queue import ensure_head_driven
from imp.engine.state import EventLog, LockHeld, ProjectLock, State
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer

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


async def serve_change_session(
    change: Change,
    *,
    run_fn: RunFn = run_change,
    hibernate: bool = True,
    tear_down: Callable[[Change], None] = tear_down_change,
    archive_left: Callable[[Change], None] = archive_conversation,
    save_left: Callable[[Change], None] = save_to_backlog,
    seed_opening: str | None = None,
    resume: bool = False,
    opening_intent: str | None = None,
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
    )

    async def _bound(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        from imp.agents.tools.wait_for_user import AwaitingUserInput

        if resume:
            log = change.event_log()
            state = State.load(change.state_dir)
            _append_workflow_resumed(log)
            # P1 crash recovery: a crashed run (status still "running" on disk)
            # never set resume_pending, so the executor would re-enter the current
            # step fresh and re-burn its work. Keep and replay it.
            if state.status == "running":
                state.resume_pending = state.step
            state.status = "running"
            state.save(change.state_dir)
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
            **first_kwargs,
        )
        while _is_interrupt_pause(result, coordinator):
            prompt_event = {"type": "awaiting_user_input",
                            "payload": {"prompt": _RESUME_PROMPT}}
            on_event(prompt_event)
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
            )
        return result

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
