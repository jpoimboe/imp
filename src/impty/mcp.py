"""Thin FastMCP adapter over `ImptyDriver`.

Standalone (`python -m impty.mcp`): a stdio MCP server hosting one
module-level `ImptyDriver`, for driving a program by hand / in dev — behavior
unchanged from the Fork-B spike, now sourced from the shared driver.

Also exposes the registration + introspection helpers (`build_server`,
`register_impty_tools`, `impty_tool_specs`) that imp's in-process provider hosts
reuse so all three surfaces present byte-identical tool names and descriptions,
all from `ImptyDriver`'s method docstrings.

The driver's calls are blocking (wait_idle, time.sleep, pty reads), so every
registered tool offloads to a thread via `asyncio.to_thread` — safe under the
standalone stdio loop and required under the providers' shared runner loop.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP

from impty.driver import ImptyDriver


@dataclass(frozen=True)
class ImptyToolSpec:
    """One impty tool: its FastMCP-introspected name/description/schema plus the
    bound `ImptyDriver` method that implements it. Hosts that can't consume a
    FastMCP instance directly (claude_code's SDK) build their tool from this."""
    name: str
    description: str
    input_schema: dict[str, Any]
    method: Callable[..., str]


def _thread_offload(method: Callable[..., str]) -> Callable[..., Any]:
    """Wrap a blocking driver method as an async tool that runs it in a thread.
    `functools.wraps` carries the method's name, docstring, and signature so
    FastMCP introspects the original tool surface."""
    @functools.wraps(method)
    async def _wrapped(**kwargs: Any) -> str:
        return await asyncio.to_thread(lambda: method(**kwargs))
    return _wrapped


def register_impty_tools(server: FastMCP, driver: ImptyDriver) -> None:
    """Register every `driver` tool method on `server` as a thread-offloaded tool."""
    for method in driver.tool_methods():
        server.tool()(_thread_offload(method))


def build_server(driver: ImptyDriver) -> FastMCP:
    """A fresh `FastMCP("impty")` with `driver`'s tools registered."""
    server = FastMCP("impty")
    register_impty_tools(server, driver)
    return server


async def impty_tool_specs(driver: ImptyDriver) -> list[ImptyToolSpec]:
    """The tool specs (name, description, JSON schema, bound method) for `driver`,
    in `tool_methods()` order. Introspected from a FastMCP build so the schema and
    description match the codex/standalone hosts exactly."""
    server = build_server(driver)
    tools_by_name = {t.name: t for t in await server.list_tools()}
    return [
        ImptyToolSpec(
            name=m.__name__,
            description=tools_by_name[m.__name__].description or "",
            input_schema=tools_by_name[m.__name__].inputSchema,
            method=m,
        )
        for m in driver.tool_methods()
    ]


_driver = ImptyDriver()
mcp = build_server(_driver)


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
