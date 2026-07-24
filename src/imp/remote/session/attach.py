"""Attach to or spawn a per-quest session daemon."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from imp.engine.env import QUEST_TMP_ENV, overlay_engine_env
from imp.engine.quest import Quest
from imp.remote.session.client import SessionClient
from imp.remote.session.discover import session_socket_path

SpawnFn = Callable[[Quest], "subprocess.Popen[bytes] | None"]
LAYOUT_MISMATCH_EXIT = 78  # sysexits EX_CONFIG: daemon hit a version downgrade, not a crash


def ensure_quest_tmp_dir(quest: Quest) -> None:
    """Idempotently create the per-quest tmp dir 0o700 on both imp-<uid> parent and <id> leaf."""
    tmp = quest.tmp_dir
    tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (tmp.parent, tmp):
        d.chmod(0o700)


def spawn_session_daemon(
    quest: Quest, *, resume: bool = False,
    non_interactive: bool = False, autonomous: bool = False,
    model: str | None = None, opening_unsandboxed: bool = False,
) -> subprocess.Popen[bytes] | None:
    argv = [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(quest.control_root),
        "--quest-id", quest.id,
    ]
    if resume:
        argv.append("--resume")
    if non_interactive:
        argv.append("--non-interactive")
    if autonomous:
        argv.append("--autonomous")
    if model is not None:
        argv += ["--model", model]
    if opening_unsandboxed:
        argv.append("--unsandboxed")
    env = overlay_engine_env(os.environ, {})
    env.pop("IMP_REMOTE_CONTROL_SOCK", None)
    env[QUEST_TMP_ENV] = str(quest.tmp_dir)
    quest.quest_dir.mkdir(parents=True, exist_ok=True)
    ensure_quest_tmp_dir(quest)
    with open(quest.quest_dir / "daemon.stderr", "wb") as stderr_file:
        return subprocess.Popen(
            argv,
            env=env,
            cwd=str(quest.control_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            start_new_session=True,
        )


class DaemonStartupError(RuntimeError):
    """A spawned session daemon exited before it bound its socket."""

    def __init__(
        self, quest_id: str, exit_code: int | None,
        stderr_path: Path, stderr_tail: str,
    ) -> None:
        self.quest_id = quest_id
        self.exit_code = exit_code
        self.stderr_path = stderr_path
        self.stderr_tail = stderr_tail
        message = (
            f"session daemon for {quest_id} exited with code {exit_code} "
            f"before it came up; see {stderr_path}")
        if stderr_tail:
            message = f"{message}\n{stderr_tail}"
        super().__init__(message)


def _read_stderr_tail(path: Path, *, max_bytes: int = 4096) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-max_bytes:].decode("utf-8", "replace").strip()


def _resolve_connect_timeout(connect_timeout: float | None) -> float:
    if connect_timeout is not None:
        return connect_timeout
    raw = os.environ.get("IMP_SESSION_CONNECT_TIMEOUT")  # env override; defaults to 60.0 when unset
    return float(raw) if raw is not None else 60.0


async def attach_or_spawn(
    quest: Quest,
    *,
    on_event: Callable[[dict[str, Any]], None],
    spawn_fn: SpawnFn = spawn_session_daemon,
    connect_timeout: float | None = None,
    lease: bool = False,
) -> SessionClient:
    socket_path = str(session_socket_path(quest.id))
    client = SessionClient(socket_path, quest.id, quest.title, on_event=on_event, lease=lease)
    try:
        await client.connect()
        return client
    except OSError:
        pass
    proc = spawn_fn(quest)
    with anyio.fail_after(_resolve_connect_timeout(connect_timeout)):
        while True:
            try:
                await client.connect()
                return client
            except OSError:
                if proc is not None and proc.poll() is not None:
                    stderr_path = quest.quest_dir / "daemon.stderr"
                    raise DaemonStartupError(
                        quest.id, proc.returncode,
                        stderr_path, _read_stderr_tail(stderr_path)) from None
                await anyio.sleep(0.05)


def build_session_attach(
    quest: Quest,
    *,
    resume: bool = False,
    non_interactive: bool = False,
    autonomous: bool = False,
    lease: bool = False,
    model: str | None = None,
    opening_unsandboxed: bool = False,
) -> Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionClient]]:
    async def attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        return await attach_or_spawn(
            quest,
            on_event=on_event,
            lease=lease,
            spawn_fn=lambda ch: spawn_session_daemon(
                ch, resume=resume,
                non_interactive=non_interactive, autonomous=autonomous,
                model=model, opening_unsandboxed=opening_unsandboxed),
        )

    return attach
