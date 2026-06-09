"""Shared test helper: strip watchdog-only heartbeats from a yielded stream.

The in-process runners now yield `tool_heartbeat` after each model-API
response and after each non-terminal tool_use. Tests that assert control
signals (agent_output / error) filter through this."""

from __future__ import annotations

from typing import Any


def control_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") != "tool_heartbeat"]
