# src/imp/cli/remote_channel.py
"""The imp-side host (channel) for an external driver process (spec §A).

A second adapter on the two existing engine ports — the `on_event` EventCallback
and the injected `user_chat_input_fn` — living beside cli/live_driver.py, never in
engine/. It owns the driver subprocess lifecycle, NDJSON framing, and the
input-race resolver. "Driver" is reserved for the external process; imp's side is
the channel/host. Best-effort by construction: no remote.driver block → unused; a
driver that fails to spawn or dies mid-run is logged and the run continues on the
local TTY. The driver can never fail a Change.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio

_log = logging.getLogger("imp.remote")

_CRASH_PROBE_SECONDS = 0.2   # bound on the spawn-then-immediately-crash check
_TERM_GRACE_SECONDS = 2.0    # SIGTERM → grace → SIGKILL window at teardown

# A driver's stderr is DEBUG by default (silent; keeps the --tui alt-screen clean),
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
    reply_timeout: float | None      # None = wait forever (like a local prompt)


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
        reply_timeout=raw.get("reply_timeout", None),
    )


def select_driver_config(
    config: Any,
    *,
    control_root: Path,
    change_id: str,
    title: str,
    env: MutableMapping[str, str],
) -> DriverConfig | None:
    """Choose the driver for one terminal Change (spec auto-attach)."""
    from imp.remote.daemon.discover import socket_is_live, socket_path
    from imp.remote.drivers import REGISTRY

    dc = driver_config_from(config)
    if dc is None:
        return None
    spath = socket_path(control_root)
    if not socket_is_live(spath):
        return dc
    env["IMP_REMOTE_CONTROL_SOCK"] = str(spath)
    env["IMP_REMOTE_CONTROL_CHANGE_ID"] = change_id
    env["IMP_REMOTE_CONTROL_TITLE"] = title
    entry = REGISTRY["daemon-connector"]
    return DriverConfig(
        command=(sys.executable, "-m", entry.module),
        events=dc.events,
        startup_timeout=dc.startup_timeout,
        reply_timeout=dc.reply_timeout,
    )


@dataclass(frozen=True, slots=True)
class RaceResult:
    source: str       # "local" | "remote"
    value: str


async def race_reply(
    local: Callable[[], Awaitable[str]],
    remote: Callable[[], Awaitable[str]],
) -> RaceResult:
    """Race two reply sources; the first to resolve wins and the loser is
    cancelled (spec §A "the race"). Pure async — tested directly with two
    injected awaitables, never through a real TTY (prompt_toolkit degrades the
    TTY race under impty). The driver *process* is untouched here: only the
    losing task is cancelled (see _channel_race_side, which swallows a dead
    channel so it never wins).

    If the local prompt raises EOFError (Ctrl-D) or KeyboardInterrupt, anyio
    wraps it in a BaseExceptionGroup.  We unwrap a lone such exception and
    re-raise it bare so the caller's `except EOFError` / `except
    KeyboardInterrupt` arms keep working (they'd miss a group)."""
    result: RaceResult | None = None

    try:
        async with anyio.create_task_group() as tg:
            async def _run(name: str, fn: Callable[[], Awaitable[str]]) -> None:
                nonlocal result
                value = await fn()
                if result is None:
                    result = RaceResult(source=name, value=value)
                    tg.cancel_scope.cancel()

            tg.start_soon(_run, "local", local)
            tg.start_soon(_run, "remote", remote)
    except BaseExceptionGroup as eg:
        if len(eg.exceptions) == 1 and isinstance(
            eg.exceptions[0], (EOFError, KeyboardInterrupt)
        ):
            raise eg.exceptions[0] from None  # bare EOFError / KeyboardInterrupt
        raise

    assert result is not None
    return result


EventCallback = Callable[[Any], None]


def compose_on_event(*callbacks: EventCallback) -> EventCallback:
    """Fan one engine event out to several on_event consumers in order (the live
    LiveDriver plus the RemoteChannel). The engine seam stays a single callback."""
    def _fan(event: Any) -> None:
        for cb in callbacks:
            cb(event)
    return _fan


async def _channel_race_side(channel: Any) -> str:
    """The driver branch of the attended race. A dead/empty channel raises
    AwaitingUserInput; swallow it and yield to the local prompt (sleep forever) so
    a dropped driver never wins the race — the local TTY remains authoritative."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    try:
        return cast(str, await channel.next_reply())
    except AwaitingUserInput:
        await anyio.sleep_forever()
        raise AssertionError("unreachable") from None


def effective_reply_fn(
    reply_input_fn: Callable[[], Awaitable[str]],
    channel: Any,
    *, non_interactive: bool,
) -> Callable[[], Awaitable[str]]:
    """Choose the reply source for a solicit window (spec §A):
      - no channel        → the local reply fn unchanged (imp behaves as today);
      - headless + channel → the local one-shot seed first (the --message seed),
        then phone-only: the driver line honoring reply_timeout (a finite value
        falls back to a pause via AwaitingUserInput);
      - attended + channel → race the local prompt against the driver line."""
    if channel is None:
        return reply_input_fn
    if non_interactive:
        async def _seed_then_phone() -> str:
            from imp.agents.tools.wait_for_user import AwaitingUserInput
            try:
                return await reply_input_fn()
            except AwaitingUserInput:
                return cast(
                    str, await channel.next_reply(timeout=channel.reply_timeout))
        return _seed_then_phone

    async def _raced() -> str:
        result = await race_reply(
            reply_input_fn, lambda: _channel_race_side(channel))
        return result.value
    return _raced


def _default_on_error(message: str) -> None:
    _log.warning("%s", message)


class RemoteChannel:
    """imp's host for one external driver process. on_event is the synchronous
    event tap (allowlist -> augment -> one best-effort NDJSON line); start/next_reply/
    stop own the subprocess + reply stream (Tasks 5-6)."""

    def __init__(
        self, *, command: tuple[str, ...], events: tuple[str, ...],
        startup_timeout: float, reply_timeout: float | None,
        id: str, title: str,
        on_error: Callable[[str], None] = _default_on_error,
    ) -> None:
        self._command = command
        self._events = events
        self._startup_timeout = startup_timeout
        self.reply_timeout = reply_timeout
        self._id = id
        self._title = title
        self._on_error = on_error
        self._proc: Any = None
        self._step: str | None = None
        self._status = "running"
        send, recv = anyio.create_memory_object_stream[str](max_buffer_size=256)
        self._reply_send = send
        self._reply_recv = recv

    @classmethod
    def from_driver_config(
        cls, dc: DriverConfig, *, id: str, title: str,
        on_error: Callable[[str], None] = _default_on_error,
    ) -> RemoteChannel:
        return cls(
            command=dc.command, events=dc.events,
            startup_timeout=dc.startup_timeout, reply_timeout=dc.reply_timeout,
            id=id, title=title, on_error=on_error)

    # -- event tap (engine on_event port) ------------------------------------
    def on_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        self._track(event)
        if not self._allowed(etype):
            return
        # Change context is namespaced under "change" so it never clobbers the
        # event's OWN id/payload fields (events carry their own log id).
        line = json.dumps({
            **event,
            "change": {
                "id": self._id, "title": self._title,
                "step": self._step, "status": self._status,
            },
        }) + "\n"
        self._write_nonblocking(line)

    def _allowed(self, etype: Any) -> bool:
        return "all" in self._events or etype in self._events

    def _track(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        payload = event.get("payload") or {}
        if etype == "step_started" and payload.get("step") is not None:
            self._step = payload["step"]
        elif etype == "workflow_paused":
            self._status = "paused"
        elif etype == "merge_blocked":
            self._status = "merge_blocked"
        elif etype == "change_merged":
            self._status = "completed"

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
        """Spawn the driver and start its reader task. Returns False (best-effort,
        run continues) if the command can't spawn or the process exits during the
        crash-probe grace (capped at startup_timeout). Healthy drivers return as
        soon as the grace confirms the process is alive — never blocked for the
        full timeout (there is no readiness handshake in the stdio contract)."""
        try:
            self._proc = subprocess.Popen(
                list(self._command),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
            self._proc = None
            return False
        tg.start_soon(self._reader_loop)
        tg.start_soon(self._stderr_drain)
        return True

    async def _stderr_drain(self) -> None:
        """Drain the driver's stderr for the channel's lifetime. The driver is
        imp's subprocess but its diagnostics are NOT imp's output: inheriting its
        stderr onto imp's controlling terminal corrupts a `--tui` alternate screen
        and interleaves with streaming output (and an undrained PIPE would fill and
        wedge a chatty driver). Lines go to the `imp.remote` logger at DEBUG —
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
                raw = await anyio.to_thread.run_sync(proc.stderr.readline)
                if raw == b"":
                    return
                level, message = _classify_stderr_line(
                    raw.decode("utf-8", "replace").rstrip("\r\n"))
                _log.log(level, "driver stderr: %s", message)
        finally:
            with contextlib.suppress(Exception):
                proc.stderr.close()

    async def _reader_loop(self) -> None:
        """Read driver stdout lines for the channel's lifetime and push each onto
        the reply stream. Outside a solicit window a buffered line simply waits in
        the stream (spec: outside a window, driver stdout is buffered/ignored). The
        blocking readline runs in a worker thread; teardown closes stdout (EOF) so
        the thread ends. On EOF the send side closes so next_reply sees a dead
        channel."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw = await anyio.to_thread.run_sync(proc.stdout.readline)
                if raw == b"":
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with contextlib.suppress(anyio.WouldBlock):
                    self._reply_send.send_nowait(line)
                    # buffer full → silently drop this (newest) incoming line
        finally:
            self._reply_send.close()

    async def next_reply(self, *, timeout: float | None = None) -> str:
        """Await one driver reply line. `timeout` (phone-only's reply_timeout) or a
        dead/closed channel raises AwaitingUserInput, which the caller maps to a
        normal pause (spec AC 8). With timeout=None and a live channel it waits
        indefinitely, like a local prompt."""
        from imp.agents.tools.wait_for_user import AwaitingUserInput
        try:
            if timeout is None:
                return await self._reply_recv.receive()
            with anyio.fail_after(timeout):
                return await self._reply_recv.receive()
        except (TimeoutError, anyio.EndOfStream, anyio.ClosedResourceError):
            raise AwaitingUserInput() from None

    async def stop(self) -> None:
        """Tear the driver down: SIGTERM, a short grace, then SIGKILL (spec AC 9).
        No-op when never spawned or already exited (idempotent). Closing stdout via
        termination ends the reader thread's blocked readline.
        Uses abandon_on_cancel=True so the grace window is honoured even if the
        process ignores SIGTERM — the abandoned thread is safe; Popen.wait() is
        internally locked."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        with anyio.move_on_after(_TERM_GRACE_SECONDS):
            await anyio.to_thread.run_sync(proc.wait, abandon_on_cancel=True)
        if proc.poll() is None:
            proc.kill()
            await anyio.to_thread.run_sync(proc.wait)
