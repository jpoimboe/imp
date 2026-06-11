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
from dataclasses import dataclass
from typing import Any

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
