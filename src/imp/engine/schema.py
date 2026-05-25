"""Parse output-schema YAML into OutputSchema; validate agent outputs."""

from __future__ import annotations

from typing import Any, Literal, cast

from imp.engine.errors import LoadError
from imp.engine.types import OutputField, OutputSchema, SourceLoc

_FieldType = Literal["string", "int", "bool", "enum", "object"]

_TYPES = {"string", "int", "bool", "enum", "object"}
_PY_TYPECHECK = {
    "string": str,
    "int": int,
    "bool": bool,
    "object": dict,
}


def parse_output_schema(raw: dict[str, Any], source: SourceLoc) -> OutputSchema:
    fields: list[OutputField] = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise LoadError(f"field '{name}' must be a mapping", source)
        if "type" not in spec:
            raise LoadError(f"field '{name}' missing 'type'", source)
        ftype = spec["type"]
        if ftype not in _TYPES:
            raise LoadError(
                f"field '{name}' has unknown type {ftype!r}",
                source,
                hint=f"valid types: {sorted(_TYPES)}",
            )
        values: tuple[str, ...] = ()
        if ftype == "enum":
            if "values" not in spec:
                raise LoadError(f"enum field '{name}' requires 'values'", source)
            values = tuple(str(v) for v in spec["values"])
        fields.append(
            OutputField(
                name=name,
                type=cast(_FieldType, ftype),
                values=values,
                required=bool(spec.get("required", True)),
            )
        )
    return OutputSchema(fields=tuple(fields), source=source)


def validate_agent_output(output: dict[str, Any], schema: OutputSchema) -> None:
    """Raise LoadError if `output` doesn't conform to `schema`.

    LoadError is reused here for symmetry; the engine wraps runtime validation
    failures as a separate event type before re-raising.
    """
    for field in schema.fields:
        if field.name not in output:
            if field.required:
                raise LoadError(
                    f"missing required field {field.name!r}", schema.source
                )
            continue
        value = output[field.name]
        if field.type == "enum":
            if value not in field.values:
                raise LoadError(
                    f"field {field.name!r}={value!r} not in {list(field.values)}",
                    schema.source,
                )
            continue
        pytype = _PY_TYPECHECK[field.type]
        if not isinstance(value, pytype):
            raise LoadError(
                f"field {field.name!r} expected {field.type}, got {type(value).__name__}",
                schema.source,
            )
