"""Recursive strict-mode schema transform for OpenAI function-call tools.

OpenAI's strict mode (spec §6.14) requires:
  1. `additionalProperties: false` at every nested object level.
  2. Every property name in `required`.
  3. Optionals expressed as nullable: `{"type": ["<orig>", "null"]}`.

The complete_step schema already satisfies (1) thanks to
`agents.tools.complete_step._normalize_anthropic_schema`. This module adds
(2) and (3) without re-deriving (1) — the transform is idempotent.

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
    """Return a new schema satisfying OpenAI strict mode.

    Original `required` is preserved structurally — every property gets added
    to `required`, but properties that were *not* in the original `required`
    list get their `type` widened to `[<orig>, "null"]`. Nested objects are
    transformed recursively.
    """
    return _walk(deepcopy(schema))


def _walk(node: dict[str, Any]) -> dict[str, Any]:
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
    `Optional[T]`."""
    t = schema.get("type")
    if isinstance(t, list):
        if "null" not in t:
            schema["type"] = [*t, "null"]
        return schema
    if isinstance(t, str):
        schema["type"] = [t, "null"]
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
