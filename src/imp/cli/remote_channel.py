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

import logging
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
