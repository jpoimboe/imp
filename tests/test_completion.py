from __future__ import annotations

from pathlib import Path

from imp.engine.completion import completion_instruction
from imp.engine.types import OutputField, OutputSchema, SourceLoc


def _schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="verdict", type="enum",
                        values=("approve", "rework"), required=True),
            OutputField(name="notes", type="string", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def test_names_the_tool() -> None:
    assert "complete_step" in completion_instruction(_schema())


def test_names_every_required_field_and_enum_value() -> None:
    text = completion_instruction(_schema())
    for token in ("status", "Success", "Error", "verdict", "approve", "rework"):
        assert token in text, token


def test_marks_optional_fields_optional() -> None:
    text = completion_instruction(_schema())
    assert "notes" in text
    assert "optional" in text.lower()


def test_instructs_tool_call_not_prose() -> None:
    text = completion_instruction(_schema())
    assert "prose" in text.lower()


def test_renders_non_enum_types() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="count", type="int", required=True),
            OutputField(name="found", type="bool", required=True),
            OutputField(name="payload", type="object", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    text = completion_instruction(schema)
    assert "`count` (int)" in text
    assert "`found` (bool)" in text
    assert "`payload` (object)" in text
    assert "optional" in text.lower()  # payload is optional
