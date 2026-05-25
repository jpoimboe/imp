"""OutputSchema → Pydantic model for response_format=."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from imp.agents.output_schema import schema_to_pydantic_model
from imp.engine.types import OutputField, OutputSchema, SourceLoc


def _schema(*fields: OutputField) -> OutputSchema:
    return OutputSchema(fields=tuple(fields), source=SourceLoc(file=Path("x"), line=1))


def test_enum_field_validates() -> None:
    schema = _schema(
        OutputField(name="status", type="enum", values=("Success", "Error")),
    )
    M = schema_to_pydantic_model(schema)
    inst = M(status="Success")
    assert inst.status == "Success"

    with pytest.raises(ValidationError):
        M(status="Bogus")


def test_required_field_missing_rejected() -> None:
    schema = _schema(
        OutputField(name="status", type="enum", values=("Success",)),
        OutputField(name="summary", type="string", required=True),
    )
    M = schema_to_pydantic_model(schema)
    with pytest.raises(ValidationError):
        M(status="Success")  # summary missing


def test_optional_field_default_none() -> None:
    schema = _schema(
        OutputField(name="status", type="enum", values=("Success",)),
        OutputField(name="notes", type="string", required=False),
    )
    M = schema_to_pydantic_model(schema)
    inst = M(status="Success")
    assert inst.notes is None


def test_int_and_bool_types() -> None:
    schema = _schema(
        OutputField(name="status", type="enum", values=("Success",)),
        OutputField(name="count", type="int"),
        OutputField(name="flag", type="bool"),
    )
    M = schema_to_pydantic_model(schema)
    inst = M(status="Success", count=3, flag=True)
    assert inst.count == 3
    assert inst.flag is True


def test_extra_fields_forbidden() -> None:
    schema = _schema(
        OutputField(name="status", type="enum", values=("Success",)),
    )
    M = schema_to_pydantic_model(schema)
    with pytest.raises(ValidationError):
        M(status="Success", junk="rejected")


def test_object_type() -> None:
    schema = _schema(
        OutputField(name="status", type="enum", values=("Success",)),
        OutputField(name="meta", type="object", required=False),
    )
    M = schema_to_pydantic_model(schema)
    inst = M(status="Success", meta={"k": "v"})
    assert inst.meta == {"k": "v"}


def test_model_name_includes_schema_hash() -> None:
    """Distinct schemas produce distinct model names so the LLM's tool
    registry doesn't collide them."""
    s1 = _schema(OutputField(name="status", type="enum", values=("Success",)))
    s2 = _schema(
        OutputField(name="status", type="enum", values=("Success",)),
        OutputField(name="extra", type="string"),
    )
    M1 = schema_to_pydantic_model(s1)
    M2 = schema_to_pydantic_model(s2)
    assert M1.__name__ != M2.__name__
