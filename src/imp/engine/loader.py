"""Workflow and agent loaders.

Build order in this file:
  Task 4 - parse_agent_file
  Tasks 5-7 - workflow YAML parsing
  Task 8 - source location tracking
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from imp.engine.errors import LoadError
from imp.engine.schema import parse_output_schema
from imp.engine.types import AgentDef, SourceLoc

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _split_frontmatter(text: str, source_path: Path) -> tuple[str, str, int]:
    """Returns (frontmatter_text, body_text, body_start_line).

    body_start_line is the 1-indexed line in the file where the body begins,
    used so prompt-template error tracebacks can be correct later.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise LoadError(
            "agent file must start with YAML frontmatter delimited by '---'",
            SourceLoc(file=source_path, line=1),
        )
    fm_text = m.group(1)
    body_text = m.group(2)
    fm_lines = fm_text.count("\n") + 1
    body_start = 1 + 1 + fm_lines + 1
    return fm_text, body_text, body_start


def parse_agent_file(path: Path) -> AgentDef:
    text = path.read_text()
    fm_text, body, _body_line = _split_frontmatter(text, path)
    try:
        fm: dict[str, Any] = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise LoadError(f"invalid YAML in frontmatter: {e}", SourceLoc(file=path, line=1)) from e
    if not isinstance(fm, dict):
        raise LoadError("frontmatter must be a mapping", SourceLoc(file=path, line=1))

    if "name" not in fm:
        raise LoadError("missing 'name' in agent frontmatter", SourceLoc(file=path, line=1))
    if "output" not in fm:
        raise LoadError("missing 'output' in agent frontmatter", SourceLoc(file=path, line=1))

    output_raw = fm["output"]
    if not isinstance(output_raw, dict):
        raise LoadError(
            "'output' must be a mapping", SourceLoc(file=path, line=1)
        )
    schema = parse_output_schema(output_raw, SourceLoc(file=path, line=1))
    tools = frozenset(fm.get("tools") or [])
    subagents = tuple(fm.get("subagents") or [])

    return AgentDef(
        name=str(fm["name"]),
        description=str(fm.get("description", "")),
        tools=tools,
        subagents=subagents,
        output_schema=schema,
        prompt_template_source=body,
        source_path=path,
        source=SourceLoc(file=path, line=1),
    )
