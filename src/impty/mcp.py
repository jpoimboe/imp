"""Thin FastMCP adapter over `ImptyDriver`.

Standalone (`python -m impty.mcp`): a stdio MCP server hosting one
module-level `ImptyDriver`, for driving a program by hand / in dev — behavior
unchanged from the Fork-B spike, now sourced from the shared driver.

Also exposes the registration + introspection helpers (`build_server`,
`register_impty_tools`, `impty_tool_specs`) that imp's in-process provider hosts
reuse. Tool **names and parameter schemas** are byte-identical across all three
surfaces (all introspected from `ImptyDriver`'s methods); the **descriptions
diverge on purpose** — the standalone stdio server keeps the full method
docstrings, while the provider surfaces (claude_code + codex) carry the thin
one-liners from `THIN_DESCRIPTIONS`.

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

# The provider surfaces (claude_code SDK + codex) show these one-liners instead
# of the full ImptyDriver docstrings; the driving-terminal-uis skill is the
# manual. Authored deliberately (not truncated from docstrings). Keyed by tool
# name and guarded by test_thin_description_mapping_covers_exactly_the_tool_methods
# so a tool added/removed without a matching entry fails the suite. The standalone
# stdio server keeps the full docstrings.
THIN_DESCRIPTIONS: dict[str, str] = {
    "start": "Spawn a program on a fresh pseudo-terminal and return its first screen.",
    "view": "Read the current terminal screen as text without sending anything.",
    "send": "Send raw keystrokes to the program, then return the new screen.",
    "send_signal": "Send a real signal (default SIGINT) to the program's process group.",
    "resize": "Resize the terminal (SIGWINCH) and return the reflowed screen.",
    "wait": "Wait for a still-working program to settle, then return the screen.",
    "snapshot": (
        "Render the current screen to a PNG (color, bold, box-drawing) and return its path."
    ),
    "stop": "Kill the current program and end the terminal session.",
}


@dataclass(frozen=True)
class ImptyToolSpec:
    """One impty tool: its FastMCP-introspected name and schema, its
    provider-surface description, and the bound `ImptyDriver` method that
    implements it. Hosts that can't consume a FastMCP instance directly
    (claude_code's SDK) build their tool from this. The `description` is the thin
    `THIN_DESCRIPTIONS` one-liner (this is a provider surface), NOT the full
    method docstring the standalone server uses; name and schema match every
    surface exactly."""

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


def register_impty_tools(
    server: FastMCP,
    driver: ImptyDriver,
    *,
    thin: bool = False,
) -> None:
    """Register every `driver` tool method on `server` as a thread-offloaded tool.
    `thin=True` sets each tool's description to its one-line `THIN_DESCRIPTIONS`
    entry (the provider surfaces); the default leaves it `None` so FastMCP falls
    back to the full method docstring (the standalone server)."""
    for method in driver.tool_methods():
        description = THIN_DESCRIPTIONS[method.__name__] if thin else None
        server.tool(description=description)(_thread_offload(method))


def build_server(driver: ImptyDriver, *, thin: bool = False) -> FastMCP:
    """A fresh `FastMCP("impty")` with `driver`'s tools registered. `thin=True`
    sources the one-line provider descriptions; the default keeps the full
    docstrings (the standalone stdio server)."""
    server = FastMCP("impty")
    register_impty_tools(server, driver, thin=thin)
    return server


def impty_tool_specs(driver: ImptyDriver) -> list[ImptyToolSpec]:
    """The tool specs (name, thin description, JSON schema, bound method) for
    `driver`, in `tool_methods()` order. Name and schema are introspected directly
    via FastMCP's `Tool.from_function` — the same code path `server.tool()` runs
    internally — so they match the codex/standalone hosts exactly, with no
    throwaway server or async round-trip. The description is deliberately the thin
    `THIN_DESCRIPTIONS` one-liner (this is a provider surface), not the full method
    docstring the standalone server carries."""
    from mcp.server.fastmcp.tools.base import Tool as MCPTool

    return [
        ImptyToolSpec(
            name=(tool := MCPTool.from_function(m)).name,
            description=THIN_DESCRIPTIONS[m.__name__],
            input_schema=tool.parameters,
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
