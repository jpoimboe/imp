# tests/test_complete_step.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.tools.complete_step import StepComplete, make_complete_step_tool
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
