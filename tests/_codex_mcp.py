"""Drive an imp Tool through the real FastMCP validation path `_register_tool`
builds — arg-model validation, `model_dump_one_level()`, then `dispatch_tool`."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.providers.codex_mcp import RunState, _register_tool


async def call_via_mcp(
    tool: Tool, *, ctx: ToolContext, state: RunState, arguments: dict[str, Any],
) -> Any:
    """Returns the tool result body. A tool error reaches a caller either as a
    returned text body or as a raised `ToolError`; both normalize to text here.
    Narrow to `ToolError` so a fault in the harness itself still raises."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    app = FastMCP("imp")
    _register_tool(app, tool, ctx=ctx, state=state)
    try:
        return await app._tool_manager.call_tool(tool.name, arguments)
    except ToolError as exc:
        return str(exc)


async def call_protocol(
    tool: Tool, *, ctx: ToolContext, state: RunState, arguments: dict[str, Any],
) -> Any:
    """Same registration path, driven over a real in-process MCP client session,
    so the caller sees the wire-level `CallToolResult` (`isError` and all)."""
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    app = FastMCP("imp")
    _register_tool(app, tool, ctx=ctx, state=state)
    async with create_connected_server_and_client_session(app._mcp_server) as session:
        return await session.call_tool(tool.name, arguments)
