"""Filter deepagents' built-in tools against the agent's declared `tools:` list.

Plan 2 Task 17 documented gap: deepagents 0.6.x has no `excluded_tools`
kwarg, so every agent silently receives every built-in. Spec §6.2 wants
strict declaration-based gating.

Strategy: for every built-in NOT declared, register a same-named stub tool
that raises UndeclaredTool when invoked. Tools added via `tools=` shadow
built-ins of the same name (verified by spike during Plan 2 Task 17).
If a future deepagents version stops shadowing, this falls back to no-op:
the agent still sees the built-in, and the error never fires.
"""

from __future__ import annotations

from collections.abc import Iterable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

# Tracks deepagents 0.6.x built-ins. Keep in sync with the runner's
# _DEEPAGENT_BUILTINS constant. `complete_step` was dropped in Plan 2.
_BUILTINS = frozenset({
    "read_file", "write_file", "edit_file", "glob", "grep", "ls",
    "execute", "write_todos", "task", "ask_user",
})


class UndeclaredTool(Exception):
    pass


class _AnyArgs(BaseModel):
    model_config = {"extra": "allow"}


def _make_stub(name: str) -> BaseTool:
    def _stub(**kwargs: object) -> str:
        raise UndeclaredTool(
            f"tool {name!r} is not declared in this agent's tools: list; "
            f"add it to the agent .md frontmatter or remove the call"
        )

    return StructuredTool.from_function(
        func=_stub,
        name=name,
        description=(
            f"Stub: {name!r} is not declared. Invocation raises UndeclaredTool."
        ),
        args_schema=_AnyArgs,
    )


def build_filtered_builtin_overrides(
    declared: Iterable[str],
) -> list[BaseTool]:
    """Return one stub BaseTool per built-in NOT in `declared`."""
    dset = set(declared)
    return [_make_stub(b) for b in sorted(_BUILTINS) if b not in dset]
