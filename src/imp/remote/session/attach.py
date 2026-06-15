from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import anyio

from imp.engine.change import Change
from imp.engine.env import overlay_engine_env
from imp.remote.session.client import SessionClient
from imp.remote.session.discover import session_socket_path

SpawnFn = Callable[[Change], None]


def spawn_session_daemon(change: Change) -> None:
    argv = [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(change.control_root),
        "--change-id", change.id,
    ]
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
) -> SessionClient:
    socket_path = str(session_socket_path(change.control_root, change.id))
    client = SessionClient(socket_path, change.id, change.title, on_event=on_event)
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
