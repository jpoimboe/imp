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

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio

_log = logging.getLogger("imp.remote")


@dataclass(frozen=True, slots=True)
class DriverConfig:
    command: tuple[str, ...]
    events: tuple[str, ...]          # ("all",) or an explicit type allowlist
    startup_timeout: float           # seconds; crash-probe ceiling at spawn
    reply_timeout: float | None      # None = wait forever (like a local prompt)


def driver_config_from(config: Any) -> DriverConfig | None:
    """Build the typed DriverConfig from a merged Config's `remote` dict, or None
    when no driver is configured. The dict is already validated + defaulted by
    providers.config._parse_remote, so every key is present here."""
    raw = (getattr(config, "remote", None) or {}).get("driver")
    if not raw:
        return None
    return DriverConfig(
        command=tuple(raw["command"]),
        events=tuple(raw["events"]),
        startup_timeout=float(raw["startup_timeout"]),
        reply_timeout=raw["reply_timeout"],
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
