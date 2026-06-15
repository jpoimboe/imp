"""Per-change session-daemon: bind run_change behind a SessionServer."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from imp.engine.change import Change
from imp.engine.executor import run_change
from imp.engine.state import ProjectLock
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer

RunFn = Callable[..., Awaitable[Any]]


async def serve_change_session(
    change: Change,
    *,
    run_fn: RunFn = run_change,
    hibernate: bool = True,
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
        return await run_fn(
            change,
            on_event=on_event,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
        )

    return await server.serve(_bound)
