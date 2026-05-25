from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.schema import parse_output_schema, validate_agent_output
from imp.engine.types import SourceLoc


def loc(line: int = 1) -> SourceLoc:
    return SourceLoc(file=Path("a.md"), line=line)


def test_parse_simple_schema() -> None:
    raw = {
        "status": {"type": "enum", "values": ["Success", "Error"]},
        "summary": {"type": "string", "required": False},
    }
    schema = parse_output_schema(raw, source=loc())
    names = {f.name for f in schema.fields}
    assert names == {"status", "summary"}
    sf = schema.status_field()
    assert sf is not None
    assert sf.values == ("Success", "Error")


def test_parse_missing_type_raises() -> None:
    raw = {"status": {"values": ["A", "B"]}}
    with pytest.raises(LoadError) as ei:
        parse_output_schema(raw, source=loc())
    assert "missing 'type'" in str(ei.value)


def test_parse_enum_missing_values_raises() -> None:
    raw = {"status": {"type": "enum"}}
    with pytest.raises(LoadError) as ei:
        parse_output_schema(raw, source=loc())
    assert "enum field 'status' requires 'values'" in str(ei.value)


def test_validate_success_status() -> None:
    schema = parse_output_schema(
        {"status": {"type": "enum", "values": ["Success", "Error"]}}, source=loc()
    )
    validate_agent_output({"status": "Success"}, schema)


def test_validate_missing_required_field() -> None:
    schema = parse_output_schema(
        {
            "status": {"type": "enum", "values": ["Success", "Error"]},
            "summary": {"type": "string"},
        },
        source=loc(),
    )
    with pytest.raises(LoadError) as ei:
        validate_agent_output({"status": "Success"}, schema)
    assert "missing required field 'summary'" in str(ei.value)


def test_validate_enum_out_of_range() -> None:
    schema = parse_output_schema(
        {"status": {"type": "enum", "values": ["Success", "Error"]}}, source=loc()
    )
    with pytest.raises(LoadError) as ei:
        validate_agent_output({"status": "Maybe"}, schema)
    assert "not in" in str(ei.value)


def test_validate_wrong_type() -> None:
    schema = parse_output_schema(
        {
            "status": {"type": "enum", "values": ["Success", "Error"]},
            "count": {"type": "int"},
        },
        source=loc(),
    )
    with pytest.raises(LoadError) as ei:
        validate_agent_output({"status": "Success", "count": "nope"}, schema)
    assert "expected int" in str(ei.value)
