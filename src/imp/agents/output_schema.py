"""Translate imp's OutputSchema (parsed from agent .md) into a Pydantic model
suitable for deepagents' response_format= parameter.

Design pivot (review #5): instead of synthesizing a tool the agent must call,
we hand the model directly to deepagents.create_deep_agent(response_format=...).
The LLM's native structured output mechanism (Anthropic tool-forced JSON,
OpenAI response_format=json_schema) generates the structured response as the
agent's terminal turn. The agent loop terminates when structured_response is
populated.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from imp.engine.types import OutputField, OutputSchema


def _field_type(field: OutputField) -> Any:
    if field.type == "string":
        return str
    if field.type == "int":
        return int
    if field.type == "bool":
        return bool
    if field.type == "enum":
        if not field.values:
            return str
        return Literal[tuple(field.values)]
    if field.type == "object":
        return dict
    raise ValueError(f"unknown OutputField type {field.type!r}")


def _schema_signature(schema: OutputSchema) -> str:
    """Short stable suffix derived from the schema's fields, used to name the
    generated Pydantic class. Avoids collisions across agents in the same
    process when multiple schemas register concurrently with langchain."""
    payload = ",".join(
        f"{f.name}:{f.type}:{'|'.join(f.values)}:{f.required}"
        for f in schema.fields
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:8]


def schema_to_pydantic_model(schema: OutputSchema) -> type[BaseModel]:
    """Return a Pydantic BaseModel matching `schema`. Extra fields are forbidden
    (defense-in-depth against models that try to fabricate keys)."""
    field_defs: dict[str, Any] = {}
    for f in schema.fields:
        py_type = _field_type(f)
        if f.required:
            field_defs[f.name] = (py_type, Field(...))
        else:
            field_defs[f.name] = (py_type | None, Field(default=None))

    model_name = f"StepOutput_{_schema_signature(schema)}"
    model: type[BaseModel] = create_model(
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **field_defs,
    )
    return model
