# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Recursive strict-mode schema transform for OpenAI function-call tools.

OpenAI's strict mode (spec §6.14) requires:
  1. `additionalProperties: false` at every nested object level.
  2. Every property name in `required`.
  3. Optionals expressed as nullable: `{"type": ["<orig>", "null"]}`.

The transform applies all three to every object node except free-form ones
(`additionalProperties: true`, Pydantic's form for `dict`), which it leaves
untouched. Strict mode cannot express an open object, so a tool whose schema
contains one must be sent non-strict (`has_free_form_object`). The transform
is idempotent.

`required` discipline: imp's `OutputSchema` tracks `required: bool` per
field. The complete_step schema reflects that via JSON schema's `required:
[...]` array. Strict mode flattens that distinction: every field must appear
in `required`, but originally-optional fields are *nullable*. Callers treat
`null` as absent.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def to_openai_strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a new schema satisfying OpenAI strict mode, except that free-form
    object nodes are left open.

    Original `required` is preserved structurally — every property gets added
    to `required`, but properties that were *not* in the original `required`
    list get their `type` widened to `[<orig>, "null"]`. Nested objects are
    transformed recursively.
    """
    return _walk(deepcopy(schema))


def has_free_form_object(schema: Any) -> bool:
    if isinstance(schema, dict):
        return _is_free_form(schema) or any(
            has_free_form_object(v) for v in schema.values())
    if isinstance(schema, list):
        return any(has_free_form_object(v) for v in schema)
    return False


def _is_free_form(node: dict[str, Any]) -> bool:
    t = node.get("type")
    is_object = t == "object" or (isinstance(t, list) and "object" in t)
    return is_object and node.get("additionalProperties") is True


def _walk(node: dict[str, Any]) -> dict[str, Any]:
    if _is_free_form(node):
        return node
    if node.get("type") == "object":
        return _walk_object(node)
    # Non-object types: recurse into items / anyOf / oneOf in case they
    # contain nested objects. V1's complete_step schemas are flat objects;
    # this branch is defense-in-depth for future schema shapes.
    for key in ("items", "additionalProperties"):
        if key in node and isinstance(node[key], dict):
            node[key] = _walk(node[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node and isinstance(node[key], list):
            node[key] = [_walk(s) if isinstance(s, dict) else s for s in node[key]]
    return node


def _walk_object(node: dict[str, Any]) -> dict[str, Any]:
    props = node.get("properties") or {}
    originally_required = set(node.get("required") or [])
    # Recurse first so nested objects are also strict.
    new_props: dict[str, Any] = {}
    for pname, pschema in props.items():
        if isinstance(pschema, dict):
            pschema = _walk(pschema)
            if pname not in originally_required:
                pschema = _nullable(pschema)
        new_props[pname] = pschema
    node["properties"] = new_props
    node["additionalProperties"] = False
    # Strict requires every property in `required`.
    node["required"] = list(new_props.keys())
    return node


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Widen `type: T` to `type: [T, "null"]`. Idempotent if already nullable,
    including the `anyOf: [{...}, {"type": "null"}]` form Pydantic emits for
    `Optional[T]`. When the property is an enum, `null` is appended to the enum
    too — otherwise the widened `["...","null"]` type permits a value the enum
    forbids and OpenAI strict mode rejects the schema."""
    t = schema.get("type")
    if isinstance(t, list):
        if "null" not in t:
            schema["type"] = [*t, "null"]
        _append_null_to_enum(schema)
        return schema
    if isinstance(t, str):
        schema["type"] = [t, "null"]
        _append_null_to_enum(schema)
        return schema
    # No top-level `type`. If it's already nullable via anyOf (Pydantic's
    # form for Optional[T]), leave it alone.
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any(
        isinstance(v, dict) and v.get("type") == "null" for v in any_of
    ):
        return schema
    # Otherwise wrap in anyOf.
    return {"anyOf": [schema, {"type": "null"}]}


def _append_null_to_enum(schema: dict[str, Any]) -> None:
    """If `schema` carries an `enum`, ensure `None` is one of its members so a
    nullable enum stays internally consistent. Idempotent."""
    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        schema["enum"] = [*enum, None]
