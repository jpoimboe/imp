# tests/test_complete_step.py
from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from imp.agents.tools.complete_step import (
    StepComplete,
    _normalize_anthropic_schema,
    make_complete_step_tool,
)
from imp.engine.types import OutputField, OutputSchema, SourceLoc


def _schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="status", type="enum", values=("Success", "Error"), required=True),
            OutputField(name="summary", type="string", required=False),
        ),
        source=SourceLoc(file=Path("agent.md"), line=1),
    )


def test_make_tool_has_schema_aligned_args() -> None:
    tool = make_complete_step_tool(_schema())
    assert tool.name == "complete_step"
    props = tool.input_schema["properties"]
    assert "status" in props
    assert "summary" in props


def test_invoke_raises_step_complete_with_args() -> None:
    tool = make_complete_step_tool(_schema())
    with pytest.raises(StepComplete) as ei:
        tool.invoke(ctx=None, args={"status": "Success", "summary": "ok"})
    assert ei.value.args_payload == {"status": "Success", "summary": "ok"}


def test_schema_normalized_for_anthropic() -> None:
    tool = make_complete_step_tool(_schema())
    s = tool.input_schema
    # No Pydantic-emitted noise keys.
    assert "title" not in s
    assert "$defs" not in s
    # Strictness against LLM fabrication.
    assert s.get("additionalProperties") is False


def test_field_description_threads_into_tool_schema() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="outcome", type="string", required=True,
                        description="one-line outcome + disposition"),
        ),
        source=SourceLoc(file=Path("agent.md"), line=1),
    )
    props = make_complete_step_tool(schema).input_schema["properties"]
    assert props["outcome"]["description"] == "one-line outcome + disposition"
    # A field with no description carries no description key.
    assert "description" not in props["status"]


_NESTED = {"k": 1, "nested": {"x": [1]}}


def _object_schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="data", type="object", required=True),
            OutputField(name="extra", type="object", required=False),
        ),
        source=SourceLoc(file=Path("agent.md"), line=1),
    )


def test_object_fields_accept_any_object() -> None:
    s = make_complete_step_tool(_object_schema()).input_schema
    jsonschema.validate({"data": _NESTED, "extra": _NESTED}, s)


def test_object_fields_still_reject_unknown_top_level_key() -> None:
    s = make_complete_step_tool(_object_schema()).input_schema
    assert s["additionalProperties"] is False
    with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
        jsonschema.validate({"data": _NESTED, "bogus": 1}, s)


def test_normalize_keeps_free_form_open_and_closes_the_rest() -> None:
    out = _normalize_anthropic_schema({
        "type": "object", "title": "Args",
        "properties": {
            "free": {"type": "object", "additionalProperties": True, "title": "Free"},
            "shut": {"type": "object", "title": "Shut"},
        },
    })
    assert out == {
        "type": "object", "additionalProperties": False,
        "properties": {
            "free": {"type": "object", "additionalProperties": True},
            "shut": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    }


def test_description_says_to_call_it_alone_after_reading_results() -> None:
    assert make_complete_step_tool(_schema()).description == (
        "Terminate this step with the structured output. "
        "Args correspond to the agent's declared output schema. "
        "Call it on its own, after reading the results of your other tool calls.")
