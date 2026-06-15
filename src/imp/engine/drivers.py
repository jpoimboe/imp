from __future__ import annotations

from collections.abc import Iterable

DRIVER_COMMANDS: dict[str, list[str]] = {
    "impty": ["python", "-m", "impty.mcp"],
}

KNOWN_DRIVERS: frozenset[str] = frozenset(DRIVER_COMMANDS)


def driver_allowed_tools(drivers: Iterable[str]) -> list[str]:
    return [f"mcp__{name}__*" for name in sorted(drivers)]


def driver_stdio_servers(drivers: Iterable[str]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "type": "stdio",
            "command": DRIVER_COMMANDS[name][0],
            "args": DRIVER_COMMANDS[name][1:],
        }
        for name in sorted(drivers)
    }
