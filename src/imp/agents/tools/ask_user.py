"""ask_user (spec §6.12). Only registered when invocation.interactive is True.

The runner injects ctx.user_input_fn before dispatching. The tool body just
calls through; the runner is responsible for routing input via prompt_toolkit
(or via the CLI's input function in tests).
"""

from __future__ import annotations

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


@imp_tool(name="ask_user", description="Ask the user a question and return their answer. Only available in interactive steps.")
def ask_user(ctx: ToolContext, question: str) -> str:
    if ctx.user_input_fn is None:
        raise RuntimeError("ask_user not available in non-interactive step")
    return ctx.user_input_fn(question)
