"""Daemon runner support helpers: event parsing, outbound drain, pidfile, and single-daemon guard."""
from __future__ import annotations

import contextlib
import json
import os
import queue
from pathlib import Path
from typing import Any

from imp.remote.daemon.discover import socket_is_live, socket_path


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
