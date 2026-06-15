"""Shared YAML helpers (line-tracking loader, source-loc derivation, scrub)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import yaml

from imp.engine.types import SourceLoc


class LineLoader(yaml.SafeLoader):
    """yaml.SafeLoader that attaches `__line__` to every mapping it produces.

    PyYAML exposes the line on the parsing event but discards it after building
    the Python object. We monkey-add it back via construct_mapping.

    Also disables YAML 1.1's "Norway problem" implicit-bool resolution for
    Yes/No/On/Off so workflow authors don't have to quote those labels.
    True/False/true/false still resolve as booleans (those rarely show up as
    intended-string labels).
    """


def _construct_mapping(loader: LineLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping = cast(dict[str, Any], loader.construct_mapping(node, deep=True))
    mapping["__line__"] = node.start_mark.line + 1  # 1-indexed
    key_lines: dict[Any, int] = {}
    for key_node, _value_node in node.value:
        key_lines[loader.construct_object(key_node)] = key_node.start_mark.line + 1
    mapping["__key_lines__"] = key_lines
    return mapping


LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)

# Strip Yes/No/On/Off from the implicit-bool resolver so they parse as the
# strings the workflow author wrote. SafeLoader inherits Resolver's class-level
# yaml_implicit_resolvers dict; we replace the bool entry for the affected
# first characters with a filtered list that excludes the Yes/No/On/Off regex.
def _disable_norway_problem() -> None:
    import re
    bool_re = re.compile(
        r"^(?:true|True|TRUE|false|False|FALSE)$"
    )
    for ch in "yYnNoO":
        existing = LineLoader.yaml_implicit_resolvers.get(ch, [])
        filtered = [
            (tag, regex) for tag, regex in existing
            if tag != "tag:yaml.org,2002:bool"
        ]
        LineLoader.yaml_implicit_resolvers[ch] = filtered
    # Replace the bool resolver on remaining chars with our stricter regex
    for ch in "tTfF":
        existing = LineLoader.yaml_implicit_resolvers.get(ch, [])
        new_entries = [
            (tag, regex) for tag, regex in existing
            if tag != "tag:yaml.org,2002:bool"
        ]
        new_entries.append(("tag:yaml.org,2002:bool", bool_re))
        LineLoader.yaml_implicit_resolvers[ch] = new_entries


_disable_norway_problem()


def loc(yaml_path: Path, node: dict[str, Any] | None) -> SourceLoc:
    """Derive a SourceLoc from a parsed-YAML mapping with `__line__` marker."""
    if isinstance(node, dict) and "__line__" in node:
        return SourceLoc(file=yaml_path, line=int(node["__line__"]))
    return SourceLoc(file=yaml_path, line=1)


def key_loc(yaml_path: Path, mapping: dict[str, Any] | None, key: str) -> SourceLoc:
    """SourceLoc for a specific KEY within a parsed mapping. Falls back to the
    mapping's own line (then line 1) when the key has no recorded position."""
    if isinstance(mapping, dict):
        key_lines = mapping.get("__key_lines__")
        if isinstance(key_lines, dict) and key in key_lines:
            return SourceLoc(file=yaml_path, line=int(key_lines[key]))
    return loc(yaml_path, mapping)


def items(d: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Iterate a parsed-YAML mapping while skipping `__line__` markers.

    Use this anywhere we iterate user-supplied mappings whose keys would
    otherwise be polluted by `LineLoader`. Failing to use it on a `decide.when`
    map causes EVERY rule to never match (the extra `__line__: N` pair makes
    the AND check fail at runtime).
    """
    for k, v in d.items():
        if isinstance(k, str) and k.startswith("__"):
            continue
        yield k, v
