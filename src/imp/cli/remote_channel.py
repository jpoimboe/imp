# src/imp/cli/remote_channel.py
"""The imp-side host (channel) for an external driver process (spec §A).

A second adapter on the two existing engine ports — the `on_event` EventCallback
and the injected `user_chat_input_fn` — living beside cli/live_driver.py, never in
engine/. It owns the driver subprocess lifecycle and NDJSON framing. "Driver" is
reserved for the external process; imp's side is the channel/host. Best-effort by
construction: no remote.driver block → unused; a
driver that fails to spawn or dies mid-run is logged and the run continues on the
local TTY. The driver can never fail a Quest.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from imp.remote.curate import EventCurator

_log = logging.getLogger("imp.remote")

_CRASH_PROBE_SECONDS = 0.2   # bound on the spawn-then-immediately-crash check
_TERM_GRACE_SECONDS = 2.0    # SIGTERM → grace → SIGKILL window at teardown

# A driver's stderr is DEBUG by default (silent; keeps the streaming output clean),
# but a driver may opt a structural failure into visibility by prefixing the line
# with one of these tags. Each is matched only at the very start of the line.
_STDERR_LEVEL_TAGS = (("ERROR:", logging.ERROR), ("WARNING:", logging.WARNING))


def _classify_stderr_line(text: str) -> tuple[int, str]:
    """Map one driver stderr line to (log_level, message). A leading severity tag
    is promoted (and stripped); everything else stays DEBUG. Only a tag at the very
    start of the line counts — a level word mid-message is not a marker."""
    for tag, level in _STDERR_LEVEL_TAGS:
        if text.startswith(tag):
            return level, text[len(tag):].lstrip()
    return logging.DEBUG, text


@dataclass(frozen=True, slots=True)
class DriverConfig:
    command: tuple[str, ...]
    events: tuple[str, ...]          # ("all",) or an explicit type allowlist
    startup_timeout: float           # seconds; crash-probe ceiling at spawn


def driver_config_from(config: Any) -> DriverConfig | None:
    from imp.remote.drivers import REGISTRY

    raw = (getattr(config, "remote", None) or {}).get("driver")
    if not raw:
        return None
    entry = REGISTRY.get(raw["name"])
    if entry is None:
        return None
    return DriverConfig(
        command=(sys.executable, "-m", entry.module),
        events=tuple(raw.get("events", entry.default_events)),
        startup_timeout=float(raw.get("startup_timeout", 10.0)),
    )


def select_driver_config(
    config: Any,
    *,
    control_root: Path,
    quest_id: str,
    title: str,
    env: MutableMapping[str, str],
) -> DriverConfig | None:
    """Choose the driver for one terminal Quest.

    A configured driver is ALWAYS run through the daemon-connector: the connector
    polls for the per-repo daemon socket and attaches whenever the daemon is up
    (including a daemon that starts after the run begins, or bounces mid-run).
    While no daemon is running the connector sits in a quiet poll loop and
    delivers nothing — so the daemon is the sole gate for remote activity: no
    daemon, no remote. The configured driver (e.g. telegram) supplies the event
    allowlist + timeouts and tells the daemon which transport to use; imp never
    spawns it directly."""
    from imp.remote.daemon.discover import socket_path
    from imp.remote.drivers import REGISTRY

    dc = driver_config_from(config)
    if dc is None:
        return None
    env["IMP_REMOTE_CONTROL_SOCK"] = str(socket_path(control_root))
    env["IMP_REMOTE_CONTROL_CHANGE_ID"] = quest_id
    env["IMP_REMOTE_CONTROL_TITLE"] = title
    entry = REGISTRY["daemon-connector"]
    return DriverConfig(
        command=(sys.executable, "-m", entry.module),
        events=dc.events,
        startup_timeout=dc.startup_timeout,
    )


EventCallback = Callable[[Any], None]


def compose_on_event(*callbacks: EventCallback) -> EventCallback:
    """Fan one engine event out to several on_event consumers in order (the live
    LiveDriver plus the RemoteChannel). The engine seam stays a single callback."""
    def _fan(event: Any) -> None:
        for cb in callbacks:
            cb(event)
    return _fan


def _default_on_error(message: str) -> None:
    _log.warning("%s", message)


def _close_proc_streams(proc: Any) -> None:
    """Close the driver's pipe fds so a GC'd Popen doesn't leak a ResourceWarning.
    Idempotent and best-effort: stderr may already be closed by the drain task, and
    stdout is DEVNULL (None). stdin is the one nothing else closes."""
    for stream in (proc.stdin, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


class RemoteChannel:
    """imp's host for one external driver process. on_event is the synchronous
    event tap (allowlist -> augment -> one best-effort NDJSON line); start/stop own
    the subprocess lifecycle (Tasks 5-6)."""

    def __init__(
        self, *, command: tuple[str, ...], events: tuple[str, ...],
        startup_timeout: float,
        id: str, title: str,
        on_error: Callable[[str], None] = _default_on_error,
    ) -> None:
        self._command = command
        self._startup_timeout = startup_timeout
        self._curator = EventCurator(id=id, title_provider=lambda: title, events=events)
        self._on_error = on_error
        self._proc: Any = None

    @classmethod
    def from_driver_config(
        cls, dc: DriverConfig, *, id: str, title: str,
        on_error: Callable[[str], None] = _default_on_error,
    ) -> RemoteChannel:
        return cls(
            command=dc.command, events=dc.events,
            startup_timeout=dc.startup_timeout,
            id=id, title=title, on_error=on_error)

    # -- event tap (engine on_event port) ------------------------------------
    def on_event(self, event: Any) -> None:
        curated = self._curator.curate(event)
        if curated is None:
            return
        self._write_nonblocking(json.dumps(curated) + "\n")

    def _write_nonblocking(self, line: str) -> None:
        """Best-effort, never fatal: write one NDJSON line to the driver's stdin.
        Sets O_NONBLOCK on the fd (idempotent) so a slow driver (full pipe →
        BlockingIOError) or a dead one (BrokenPipeError) is dropped, not awaited —
        on_event must never stall or fail the run (spec AC 3)."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        try:
            fd = proc.stdin.fileno()
            os.set_blocking(fd, False)
            os.write(fd, line.encode("utf-8"))
        except (BlockingIOError, BrokenPipeError, OSError):
            pass

    # -- lifecycle -----------------------------------------------------------
    async def start(self, tg: Any) -> bool:
        """Spawn the driver and start its stderr drain task. Returns False (best-effort,
        run continues) if the command can't spawn or the process exits during the
        crash-probe grace (capped at startup_timeout). Healthy drivers return as
        soon as the grace confirms the process is alive — never blocked for the
        full timeout (there is no readiness handshake in the stdio contract)."""
        try:
            self._proc = subprocess.Popen(
                list(self._command),
                # stdout unused; DEVNULL so an undrained pipe can't wedge the driver.
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
        except FileNotFoundError:
            _log.debug("remote driver %r not found; notifications disabled",
                       self._command[0])
            self._proc = None
            return False
        except OSError as exc:
            self._on_error(f"driver failed to spawn ({self._command[0]!r}): {exc}")
            self._proc = None
            return False
        os.set_blocking(self._proc.stdin.fileno(), False)
        await anyio.sleep(min(self._startup_timeout, _CRASH_PROBE_SECONDS))
        if self._proc.poll() is not None:
            self._on_error(
                f"driver exited during startup (rc {self._proc.returncode})")
            _close_proc_streams(self._proc)
            self._proc = None
            return False
        tg.start_soon(self._stderr_drain)
        return True

    async def _stderr_drain(self) -> None:
        """Drain the driver's stderr for the channel's lifetime. The driver is
        imp's subprocess but its diagnostics are NOT imp's output: inheriting its
        stderr onto imp's controlling terminal interleaves with the streaming
        output (and an undrained PIPE would fill and wedge a chatty driver). Lines
        go to the `imp.remote` logger at DEBUG —
        default-silent (imp installs no handler), recoverable with debug logging.
        A driver may tag a structural failure (`WARNING:`/`ERROR:`) to promote that
        line to a level Python's lastResort handler surfaces by default (see
        _classify_stderr_line). Teardown closes stderr (EOF) so the worker thread
        ends."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                try:
                    raw = await anyio.to_thread.run_sync(proc.stderr.readline)
                except ValueError:
                    # stop() closed stderr before this drain's readline landed
                    # (late scheduling under load): a closed stream is EOF, and
                    # must never raise into the run's task group.
                    return
                if raw == b"":
                    return
                level, message = _classify_stderr_line(
                    raw.decode("utf-8", "replace").rstrip("\r\n"))
                _log.log(level, "driver stderr: %s", message)
        finally:
            with contextlib.suppress(Exception):
                proc.stderr.close()

    async def stop(self) -> None:
        """Tear the driver down: SIGTERM, a short grace, then SIGKILL (spec AC 9).
        No-op when never spawned; when the driver already exited, skips signalling
        but still closes the pipe fds (idempotent). Closing stderr via termination
        ends the stderr-drain thread's blocked readline.
        Uses abandon_on_cancel=True so the grace window is honoured even if the
        process ignores SIGTERM — the abandoned thread is safe; Popen.wait() is
        internally locked."""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                with anyio.move_on_after(_TERM_GRACE_SECONDS):
                    await anyio.to_thread.run_sync(proc.wait, abandon_on_cancel=True)
                if proc.poll() is None:
                    proc.kill()
                    await anyio.to_thread.run_sync(proc.wait)
        finally:
            # Close stdin (and stderr, if the drain task didn't) so the Popen can be
            # GC'd without a ResourceWarning — including the path where the driver
            # already exited on its own before stop() was called.
            _close_proc_streams(proc)
