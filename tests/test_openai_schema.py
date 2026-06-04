"""Strict-mode JSON schema transform for OpenAI function-call tools (spec §6.14).

The transform is the load-bearing translation layer for V1 OpenAI support.
Bugs here turn into 400s from OpenAI at runtime, so coverage stays close to
each rule the strict-mode spec enforces.
"""

from __future__ import annotations

from imp.providers._openai_schema import to_openai_strict


def test_required_property_unchanged() -> None:
    """A field that was originally required stays required and keeps its type."""
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
    }
    out = to_openai_strict(src)
    assert out["properties"]["status"] == {"type": "string"}
    assert out["required"] == ["status"]
    assert out["additionalProperties"] is False


def test_optional_property_becomes_nullable_and_required() -> None:
    """An originally-optional field becomes nullable AND appears in required."""
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"type": "string"},
            "note":   {"type": "string"},
        },
        "required": ["status"],
    }
    out = to_openai_strict(src)
    assert out["properties"]["note"]["type"] == ["string", "null"]
    assert out["properties"]["status"]["type"] == "string"
    assert set(out["required"]) == {"status", "note"}


def test_nested_object_recursively_normalized() -> None:
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "inner_req": {"type": "string"},
                    "inner_opt": {"type": "int"},
                },
                "required": ["inner_req"],
            },
        },
        "required": ["outer"],
    }
    out = to_openai_strict(src)
    outer = out["properties"]["outer"]
    assert outer["additionalProperties"] is False
    assert set(outer["required"]) == {"inner_req", "inner_opt"}
    assert outer["properties"]["inner_opt"]["type"] == ["int", "null"]


def test_enum_field_stays_enum() -> None:
    """Enum-only properties (no top-level `type`) are wrapped in anyOf when optional."""
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "verdict": {"enum": ["approve", "rework"]},
        },
        "required": [],
    }
    out = to_openai_strict(src)
    # No `type` to widen; nullable wrapper is anyOf.
    verdict = out["properties"]["verdict"]
    assert "anyOf" in verdict
    assert {"type": "null"} in verdict["anyOf"]
    assert out["required"] == ["verdict"]


def test_idempotent() -> None:
    """Running the transform twice produces the same output as running it once."""
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    once = to_openai_strict(src)
    twice = to_openai_strict(once)
    assert twice == once


def test_complete_step_schema_round_trip() -> None:
    """Sanity: pull a real complete_step schema through the transform end-to-end."""
    from pathlib import Path

    from imp.agents.tools.complete_step import make_complete_step_tool
    from imp.engine.types import OutputField, OutputSchema, SourceLoc

    schema = OutputSchema(
        fields=(
            OutputField(
                name="status", type="enum",
                values=("Success", "Error"), required=True,
            ),
            OutputField(name="note", type="string", required=False),
            OutputField(name="count", type="int", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    tool = make_complete_step_tool(schema)
    strict = to_openai_strict(tool.input_schema)
    assert set(strict["required"]) == {"status", "note", "count"}
    assert strict["additionalProperties"] is False
    # Optional fields are nullable (Pydantic emits Optional[T] as
    # anyOf: [{type: T}, {type: null}]; the transform recognizes that form
    # and leaves it alone rather than double-wrapping).
    for opt in ("note", "count"):
        prop = strict["properties"][opt]
        assert _is_nullable(prop), f"{opt} not nullable: {prop}"


def test_optional_enum_gets_null_appended() -> None:
    """An optional enum, widened to nullable, must include null in its enum
    list — otherwise OpenAI strict mode rejects the schema."""
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "subagent_name": {"type": "string"},
            "tier": {"type": "string", "enum": ["low", "high"]},
        },
        "required": ["subagent_name"],   # model_tier optional
    }
    out = to_openai_strict(src)
    tier = out["properties"]["tier"]
    assert tier["type"] == ["string", "null"]
    assert None in tier["enum"]
    assert tier["enum"] == ["low", "high", None]


def test_nullable_enum_append_idempotent() -> None:
    """Running the transform twice does not append null twice."""
    src = {
        "type": "object", "additionalProperties": False,
        "properties": {"tier": {"type": "string", "enum": ["low"]}},
        "required": [],
    }
    once = to_openai_strict(src)
    twice = to_openai_strict(once)
    assert twice == once
    assert once["properties"]["tier"]["enum"] == ["low", None]


def _is_nullable(prop: dict) -> bool:
    """A property is nullable if its `type` is a list including 'null', OR
    if it has an `anyOf` containing a `{"type": "null"}` variant."""
    t = prop.get("type")
    if isinstance(t, list) and "null" in t:
        return True
    if isinstance(t, str) and t == "null":
        return True  # not really a property but keep the check robust
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        return any(isinstance(v, dict) and v.get("type") == "null" for v in any_of)
    return False
