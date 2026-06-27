"""Attach to or spawn a per-change session daemon."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from imp.engine.change import Change
from imp.engine.env import overlay_engine_env
from imp.remote.session.client import SessionClient
from imp.remote.session.discover import session_socket_path

SpawnFn = Callable[[Change], None]


def spawn_session_daemon(
    change: Change, *, seed_opening: str | None = None, resume: bool = False,
    opening_intent: str | None = None, scope_only: bool = False,
    non_interactive: bool = False, autonomous: bool = False,
) -> None:
    argv = [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(change.control_root),
        "--change-id", change.id,
    ]
    if seed_opening is not None:
        argv += [f"--seed-opening={seed_opening}"]
    if opening_intent is not None:
        argv += [f"--opening-intent={opening_intent}"]
    if resume:
        argv.append("--resume")
    if scope_only:
        argv.append("--scope-only")
    if non_interactive:
        argv.append("--non-interactive")
    if autonomous:
        argv.append("--autonomous")
    env = overlay_engine_env(os.environ, {})
    env.pop("IMP_REMOTE_CONTROL_SOCK", None)
    subprocess.Popen(
        argv,
        env=env,
        cwd=str(change.control_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def attach_or_spawn(
    change: Change,
    *,
    on_event: Callable[[dict[str, Any]], None],
    spawn_fn: SpawnFn = spawn_session_daemon,
    connect_timeout: float = 10.0,
    lease: bool = False,
) -> SessionClient:
    socket_path = str(session_socket_path(change.control_root, change.id))
    client = SessionClient(socket_path, change.id, change.title, on_event=on_event, lease=lease)
    try:
        await client.connect()
        return client
    except OSError:
        pass
    spawn_fn(change)
    with anyio.fail_after(connect_timeout):
        while True:
            try:
                await client.connect()
                return client
            except OSError:
                await anyio.sleep(0.05)


def build_session_attach(
    change: Change,
    *,
    seed_opening: str | None = None,
    resume: bool = False,
    opening_intent: str | None = None,
    scope_only: bool = False,
    non_interactive: bool = False,
    autonomous: bool = False,
    lease: bool = False,
) -> Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionClient]]:
    async def attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        return await attach_or_spawn(
            change,
            on_event=on_event,
            lease=lease,
            spawn_fn=lambda ch: spawn_session_daemon(
                ch, seed_opening=seed_opening, resume=resume,
                opening_intent=opening_intent, scope_only=scope_only,
                non_interactive=non_interactive, autonomous=autonomous),
        )

    return attach
