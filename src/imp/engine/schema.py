"""Parse output-schema YAML into OutputSchema; validate agent outputs."""

from __future__ import annotations

from typing import Any, Literal, cast

from imp.engine._yaml import items
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
    for name, spec in items(raw):
        if not isinstance(spec, dict):
            raise LoadError(f"field {name!r} must be a mapping", source)
        if "type" not in spec:
            raise LoadError(f"field {name!r} missing 'type'", source)
        ftype = spec["type"]
        if ftype not in _TYPES:
            raise LoadError(
                f"field {name!r} has unknown type {ftype!r}",
                source,
                hint=f"valid types: {', '.join(sorted(_TYPES))}",
            )
        values: tuple[str, ...] = ()
        if ftype == "enum":
            if "values" not in spec:
                raise LoadError(f"enum field {name!r} requires 'values'", source)
            raw_values = spec["values"]
            if not isinstance(raw_values, list) or not raw_values:
                raise LoadError(
                    f"enum field {name!r} 'values' must be a non-empty list",
                    source,
                )
            for v in raw_values:
                if not isinstance(v, str):
                    raise LoadError(
                        f"enum field {name!r} values must be strings; "
                        f"got {type(v).__name__} {v!r}",
                        source,
                        hint="quote numeric/boolean values: [\"1\", \"2\"]",
                    )
            values = tuple(raw_values)
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
        # bool is a subclass of int — explicitly reject bool when int is required
        if not isinstance(value, pytype) or (field.type == "int" and isinstance(value, bool)):
            raise LoadError(
                f"field {field.name!r} expected {field.type}, got {type(value).__name__}",
                schema.source,
            )
