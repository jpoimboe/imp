from __future__ import annotations

from collections.abc import Iterable

# impty is hosted in-process by each provider — there is no stdio command table.
KNOWN_DRIVERS: frozenset[str] = frozenset({"impty"})


def driver_allowed_tools(drivers: Iterable[str]) -> list[str]:
    return [f"mcp__{name}__*" for name in sorted(drivers)]
