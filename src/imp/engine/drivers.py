from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from imp.engine.selfhost import selfhost_full_env, selfhost_launch

DRIVER_COMMANDS: dict[str, list[str]] = {
    "impty": ["python", "-m", "impty.mcp"],
}

KNOWN_DRIVERS: frozenset[str] = frozenset(DRIVER_COMMANDS)


def driver_allowed_tools(drivers: Iterable[str]) -> list[str]:
    return [f"mcp__{name}__*" for name in sorted(drivers)]


def driver_stdio_servers(
    drivers: Iterable[str], *, project_root: Path | None = None,
) -> dict[str, dict[str, object]]:
    servers: dict[str, dict[str, object]] = {}
    for name in sorted(drivers):
        cfg: dict[str, object] = {
            "type": "stdio",
            "command": DRIVER_COMMANDS[name][0],
            "args": DRIVER_COMMANDS[name][1:],
        }
        if name == "impty" and project_root is not None:
            launch = selfhost_launch(project_root)
            if launch is not None:
                cfg["env"] = selfhost_full_env(launch, os.environ)
        servers[name] = cfg
    return servers
