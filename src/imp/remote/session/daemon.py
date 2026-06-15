"""Per-change session-daemon: bind run_change behind a SessionServer."""
from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from imp.engine.abandon import tear_down_change
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import ProjectLock
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer

RunFn = Callable[..., Awaitable[Any]]

_RESUME_PROMPT = "paused — type a message to redirect, or detach to leave it paused"


def _is_interrupt_pause(result: Any, coordinator: Any) -> bool:
    return result is RunResult.PAUSED and coordinator.should_pause


async def serve_change_session(
    change: Change,
    *,
    run_fn: RunFn = run_change,
    hibernate: bool = True,
    tear_down: Callable[[Change], None] = tear_down_change,
) -> Any:
    # Shared coordinator: socket `interrupt` control frame → run_change's pause check.
    from imp.cli.signals import InterruptCoordinator

    coordinator = InterruptCoordinator()
    socket_path = str(session_socket_path(change.control_root, change.id))
    server = SessionServer(
        socket_path,
        coordinator=coordinator,
        lock=ProjectLock(change.change_dir / "lock"),
        hibernate=hibernate,
    )
    await server.open()

    async def _bound(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        from imp.agents.tools.wait_for_user import AwaitingUserInput

        result = await run_fn(
            change,
            on_event=on_event,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
        )
        while _is_interrupt_pause(result, coordinator):
            prompt_event = {"type": "awaiting_user_input",
                            "payload": {"prompt": _RESUME_PROMPT}}
            server.set_pending_prompt(prompt_event)
            on_event(prompt_event)
            try:
                reply = await user_chat_input_fn("")
            except AwaitingUserInput:
                return result
            finally:
                server.set_pending_prompt(None)
            coordinator.reset()
            if reply.strip():
                coordinator.enqueue_interject(reply)
            result = await run_fn(
                change,
                on_event=on_event,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
            )
        return result

    result = await server.serve(_bound)
    if server.abandoned:
        # cwd is control_root, not the worktree, so worktree removal is safe
        await anyio.to_thread.run_sync(tear_down, change)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imp-session-daemon")
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--change-id", required=True)
    ns = parser.parse_args(argv)
    change = Change.load(Path(ns.control_root), ns.change_id)
    anyio.run(serve_change_session, change)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
