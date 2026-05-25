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


def test_parse_field_spec_must_be_mapping() -> None:
    raw = {"status": "not a mapping"}
    with pytest.raises(LoadError) as ei:
        parse_output_schema(raw, source=loc())
    assert "must be a mapping" in str(ei.value)


def test_parse_unknown_type_with_hint() -> None:
    raw = {"x": {"type": "tuple"}}
    with pytest.raises(LoadError) as ei:
        parse_output_schema(raw, source=loc())
    msg = str(ei.value)
    assert "unknown type" in msg
    assert "valid types:" in msg
    # Stable hint format: comma-separated, not Python list repr
    assert "[" not in msg.split("valid types:")[1]


def test_parse_empty_enum_values_rejected() -> None:
    raw = {"status": {"type": "enum", "values": []}}
    with pytest.raises(LoadError) as ei:
        parse_output_schema(raw, source=loc())
    assert "non-empty" in str(ei.value)


def test_parse_non_string_enum_values_rejected() -> None:
    raw = {"code": {"type": "enum", "values": [200, 404]}}
    with pytest.raises(LoadError) as ei:
        parse_output_schema(raw, source=loc())
    assert "values must be strings" in str(ei.value)


def test_validate_bool_rejected_for_int_field() -> None:
    schema = parse_output_schema(
        {"count": {"type": "int"}}, source=loc()
    )
    with pytest.raises(LoadError) as ei:
        validate_agent_output({"count": True}, schema)
    assert "expected int" in str(ei.value)


def test_validate_int_passes_int_field() -> None:
    schema = parse_output_schema(
        {"count": {"type": "int"}}, source=loc()
    )
    validate_agent_output({"count": 42}, schema)  # no raise
