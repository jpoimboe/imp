"""write_todos (spec §6.6). Replaces ctx.todos; per-step in-memory only."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


@imp_tool(name="write_todos", description="Replace the agent's in-memory todo list with the given list. Each todo: {text, status}.")
def write_todos(ctx: ToolContext, todos: list[dict[str, Any]]) -> str:
    ctx.todos = list(todos)
    return f"recorded {len(todos)} todos"
