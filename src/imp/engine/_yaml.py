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
    """


def _construct_mapping(loader: LineLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping = cast(dict[str, Any], loader.construct_mapping(node, deep=True))
    mapping["__line__"] = node.start_mark.line + 1  # 1-indexed
    return mapping


LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def loc(yaml_path: Path, node: dict[str, Any] | None) -> SourceLoc:
    """Derive a SourceLoc from a parsed-YAML mapping with `__line__` marker."""
    if isinstance(node, dict) and "__line__" in node:
        return SourceLoc(file=yaml_path, line=int(node["__line__"]))
    return SourceLoc(file=yaml_path, line=1)


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
