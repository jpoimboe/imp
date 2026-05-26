"""complete_step (spec §6.14). Synthesized per-agent from the agent's
output_schema. Args ARE the schema. Body raises StepComplete; runner catches.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, create_model

from imp.agents.tools import Tool
from imp.engine.types import OutputField, OutputSchema


class StepComplete(Exception):
    def __init__(self, args_payload: dict[str, Any]) -> None:
        super().__init__("step complete")
        self.args_payload = args_payload


def _py_type(f: OutputField) -> Any:
    if f.type == "string":
        return str
    if f.type == "int":
        return int
    if f.type == "bool":
        return bool
    if f.type == "object":
        return dict
    if f.type == "enum":
        if not f.values:
            return str
        return Literal[tuple(f.values)]  # type: ignore[valid-type]
    raise ValueError(f"unknown field type {f.type!r}")


def _build_schema(output: OutputSchema) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for f in output.fields:
        t = _py_type(f)
        if f.required:
            fields[f.name] = (t, Field(...))
        else:
            fields[f.name] = (t | None, Field(default=None))
    model = create_model("CompleteStepArgs", **fields)  # type: ignore[call-overload]
    return _normalize_anthropic_schema(model.model_json_schema())


def _normalize_anthropic_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic emits keys (`title`, `$defs`, optional `additionalProperties: true`)
    that Anthropic's tool-schema validator may not accept. Strip the noise and
    force `additionalProperties: false` so the LLM can't fabricate extra keys.

    Recursive — covers nested object properties.
    """
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in ("title", "$defs")}
    if out.get("type") == "object":
        out["additionalProperties"] = False
        props = out.get("properties") or {}
        out["properties"] = {k: _normalize_anthropic_schema(v) for k, v in props.items()}
    return out


def make_complete_step_tool(output_schema: OutputSchema) -> Tool:
    schema = _build_schema(output_schema)

    def _invoke(ctx: Any, **kwargs: Any) -> str:
        raise StepComplete(kwargs)

    return Tool(
        name="complete_step",
        description="Terminate this step with the structured output. Args correspond to the agent's declared output schema.",
        input_schema=schema,
        fn=_invoke,
    )
