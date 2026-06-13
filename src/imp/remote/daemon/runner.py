"""Daemon runner: event parsing, outbound drain, pidfile, single-daemon guard, and run_daemon."""
from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import anyio

from imp.remote.daemon import discover, spawn
from imp.remote.daemon.control import ControlPlane
from imp.remote.daemon.discover import socket_is_live, socket_path
from imp.remote.daemon.server import SouthHub
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter


class DaemonAlreadyRunning(Exception):
    """A daemon is already accepting on this repo's socket (single daemon per repo)."""


def parse_event(line: str) -> dict[str, Any] | None:
    """One forwarded south NDJSON line → event dict, or None for non-JSON or non-object."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def drain_outbound(adapter: Any, outbound: queue.Queue[str]) -> None:
    """Flush every queued south line into the adapter; garbage lines are dropped."""
    while True:
        try:
            line = outbound.get_nowait()
        except queue.Empty:
            return
        event = parse_event(line)
        if event is not None:
            adapter.on_event(event)


def pid_path(control_root: Path) -> Path:
    return control_root / ".imp" / "remote-control.pid"


def write_pid(control_root: Path) -> None:
    pid_path(control_root).write_text(str(os.getpid()))


def read_pid(control_root: Path) -> int | None:
    try:
        return int(pid_path(control_root).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def remove_pid(control_root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        pid_path(control_root).unlink()


def check_not_running(control_root: Path) -> None:
    """Raise DaemonAlreadyRunning if a daemon is already live on this repo's socket."""
    if socket_is_live(socket_path(control_root)):
        raise DaemonAlreadyRunning(
            f"a remote-control daemon is already running for {control_root}")


def _make_telegram_adapter(
    env: Mapping[str, str], submit: Callable[[str, str], None],
) -> TelegramNorthAdapter:
    """Default north adapter — Telegram, refusing without an allowlist."""
    return TelegramNorthAdapter.from_env(dict(env), submit)


def _adapter_worker(
    adapter: Any, outbound: queue.Queue[str], stop: threading.Event,
) -> None:
    """Flush queued south events then run one poll cycle, until stop is set."""
    while not stop.is_set():
        drain_outbound(adapter, outbound)
        adapter.poll_once()


async def _relay_replies(
    replies: queue.Queue[tuple[str, str]], control: ControlPlane,
    stop: threading.Event,
) -> None:
    """Route each inbound reply submitted by the adapter to its Change."""
    def _get() -> tuple[str, str] | None:
        while not stop.is_set():
            try:
                return replies.get(timeout=0.05)
            except queue.Empty:
                continue
        return None

    while True:
        result = await anyio.to_thread.run_sync(_get)
        if result is None:
            return
        change_id, text = result
        await control.reply(change_id, text)


async def _await_shutdown_signals(shutdown: anyio.Event) -> None:
    """Set `shutdown` on the first SIGTERM/SIGINT (spec: a clean SIGTERM drains)."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _signum in signals:
            shutdown.set()
            return


async def run_daemon(
    control_root: Path,
    *,
    env: Mapping[str, str] | None = None,
    make_adapter: Callable[
        [Mapping[str, str], Callable[[str, str], None]], Any] = _make_telegram_adapter,
    resume: Callable[[str, str], None] | None = None,
    create: Callable[[str], None] | None = None,
    abandon: Callable[[str], None] | None = None,
    list_changes: Callable[[], list[discover.ChangeInfo]] | None = None,
    install_signals: bool = True,
    shutdown: anyio.Event | None = None,
) -> None:
    """Run the daemon until shutdown: bind socket, own adapter, relay both directions."""
    env = os.environ if env is None else env
    shutdown = anyio.Event() if shutdown is None else shutdown
    resume = resume or (lambda cid, text: spawn.resume_change(control_root, cid, text))
    create = create or (lambda prompt: spawn.create_change(control_root, prompt))
    abandon = abandon or (lambda cid: spawn.abandon_change(control_root, cid))
    list_changes = list_changes or (lambda: discover.list_changes(control_root))

    spath = socket_path(control_root)
    outbound: queue.Queue[str] = queue.Queue()
    replies: queue.Queue[tuple[str, str]] = queue.Queue()
    stop = threading.Event()

    hub = SouthHub(str(spath), on_event=lambda cid, line: outbound.put(line))
    control = ControlPlane(
        hub=hub, resume=resume, create=create,
        abandon=abandon, list_changes=list_changes)
    adapter = make_adapter(env, lambda cid, text: replies.put((cid, text)))

    with contextlib.suppress(FileNotFoundError):
        spath.unlink()   # clear a stale socket file (guard already proved not live)
    try:
        write_pid(control_root)
        await hub.open()
        async with anyio.create_task_group() as tg:
            tg.start_soon(hub.serve)
            tg.start_soon(_relay_replies, replies, control, stop)
            tg.start_soon(anyio.to_thread.run_sync,
                          _adapter_worker, adapter, outbound, stop)
            if install_signals:
                tg.start_soon(_await_shutdown_signals, shutdown)
            await shutdown.wait()
            stop.set()
            await hub.close()       # triggers ClosedResourceError in hub.serve
            await anyio.sleep(0)    # let the hub.serve task see it before we cancel
            tg.cancel_scope.cancel()
    finally:
        await hub.close()
        with contextlib.suppress(FileNotFoundError):
            spath.unlink()
        remove_pid(control_root)


def start_daemon(control_root: Path) -> int:
    """`imp remote-control start`: reject a second live daemon, then run in foreground."""
    from imp.remote.drivers.telegram import ConfigError
    try:
        check_not_running(control_root)
    except DaemonAlreadyRunning:
        print("imp: a remote-control daemon is already running for this repo",
              file=sys.stderr)
        return 2
    try:
        anyio.run(run_daemon, control_root)
    except ConfigError as exc:
        print(f"imp: remote-control not configured: {exc}", file=sys.stderr)
        return 1
    return 0


def stop_daemon(control_root: Path) -> int:
    """`imp remote-control stop`: SIGTERM the recorded daemon pid."""
    pid = read_pid(control_root)
    if pid is None:
        print("imp: no remote-control daemon running for this repo", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid(control_root)
        print("imp: no remote-control daemon running for this repo", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"imp: no permission to signal the daemon (pid {pid})", file=sys.stderr)
        return 1
    print(f"Sent SIGTERM to remote-control daemon (pid {pid}).")
    return 0
