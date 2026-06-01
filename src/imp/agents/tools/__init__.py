"""@imp_tool decorator + global tool registry (spec §6.5).

Tools are Python functions registered once at import-time. Each runner
filters the registry to the agent's declared tools at step start.

Schema derivation:
  - Use pydantic to build an args_schema from the function's type hints,
    then export its JSON schema. ctx (positional 0) is excluded.
  - Inferred JSON schema is the Anthropic shape (`{type:object,
    properties:{…}, required:[…]}`). Runners adapt to provider shape.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field, create_model


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]

    def invoke(self, ctx: Any, args: dict[str, Any]) -> Any:
        result = self.fn(ctx, **args)
        if inspect.iscoroutine(result):
            raise TypeError(
                f"tool {self.name!r} is async; use async_invoke from runner"
            )
        return result

    async def async_invoke(self, ctx: Any, args: dict[str, Any]) -> Any:
        """Await-friendly invocation. Sync tools return their value; async
        tools (`async def`) get awaited transparently."""
        result = self.fn(ctx, **args)
        if inspect.iscoroutine(result):
            return await result
        return result


_REGISTRY: dict[str, Tool] = {}


def _build_input_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    fields: dict[str, Any] = {}
    for i, (name, param) in enumerate(sig.parameters.items()):
        if i == 0 and name == "ctx":
            continue
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, Field(default=default) if default is not ... else Field(...))

    if not fields:
        return {"type": "object", "properties": {}, "required": []}

    model = create_model(f"{fn.__name__}_args", **fields)
    schema: dict[str, Any] = model.model_json_schema()
    return schema


def imp_tool(*, name: str, description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the wrapped function as `name` in the global registry."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        schema = _build_input_schema(fn)
        _REGISTRY[name] = Tool(
            name=name, description=description, input_schema=schema, fn=fn,
        )
        return fn

    return decorate


def get_registry() -> dict[str, Tool]:
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Test-only — reset between tests that exercise registration directly."""
    _REGISTRY.clear()


def register_tool(tool: Tool) -> None:
    """Programmatic registration (used by synthesized per-agent `complete_step`)."""
    _REGISTRY[tool.name] = tool


__all__ = ["Tool", "clear_registry", "get_registry", "imp_tool", "register_tool"]


# DO NOT REORDER: `_REGISTRY` and `imp_tool` must be defined ABOVE this point
# so the side-effect imports inside `_register_builtins()` can resolve them.
# Each built-in tool module does `from imp.agents.tools import imp_tool` mid-init,
# which works only because `imp_tool` is already bound when the import runs.
# Note: complete_step is NOT auto-imported here — it's synthesized per-agent
# by make_complete_step_tool(...) and registered via Tool, not @imp_tool.
def _register_builtins() -> None:
    """Import all built-in tool modules so their @imp_tool decorations fire."""
    from imp.agents.tools import (  # noqa: F401
        bash,
        commit,
        fs,
        git_diff,
        nav,
        query_events,
        rename_change,
        todos,
    )


_register_builtins()
