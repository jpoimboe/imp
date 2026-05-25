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
from imp.engine.types import (
    Action,
    AgentDef,
    Continue,
    Goto,
    Pause,
    Phase,
    Retry,
    SourceLoc,
    Step,
    Workflow,
    WorkflowDefaults,
)

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


# ── Workflow parsing (Task 5) ──────────────────────────────────────────────

DEFAULT_MAX_RETRIES = 3
DEFAULT_ON_MAX_RETRIES: Action = Pause()


def _parse_action(raw: dict[str, Any] | None, source: SourceLoc) -> Action:
    if raw is None:
        raise LoadError("missing action", source)
    if not isinstance(raw, dict):
        raise LoadError(f"action must be a mapping, got {type(raw).__name__}", source)
    kind = raw.get("kind")
    if kind == "continue":
        return Continue()
    if kind == "retry":
        return Retry()
    if kind == "pause":
        return Pause()
    if kind == "goto":
        if "target" not in raw:
            raise LoadError("goto action missing 'target'", source)
        return Goto(target=str(raw["target"]))
    raise LoadError(
        f"unknown action kind {kind!r}",
        source,
        hint="valid kinds: continue, retry, pause, goto",
    )


def _resolve_max_retries(
    raw: dict[str, Any], default: int, source: SourceLoc, where: str
) -> int:
    """Resolve max_retries with null tolerance; reject non-int values cleanly."""
    if "max_retries" not in raw:
        return default
    val = raw["max_retries"]
    if val is None:
        return default
    if not isinstance(val, int) or isinstance(val, bool):
        raise LoadError(
            f"{where}: 'max_retries' must be an integer, got {type(val).__name__}",
            source,
        )
    if val < 0:
        raise LoadError(f"{where}: 'max_retries' must be >= 0", source)
    return val


def _parse_defaults(
    raw: dict[str, Any] | None, source: SourceLoc
) -> WorkflowDefaults:
    if raw is None:
        return WorkflowDefaults(
            max_retries=DEFAULT_MAX_RETRIES, on_max_retries=DEFAULT_ON_MAX_RETRIES
        )
    max_retries = _resolve_max_retries(raw, DEFAULT_MAX_RETRIES, source, "defaults")
    on_max = (
        _parse_action(raw["on_max_retries"], source)
        if "on_max_retries" in raw
        else DEFAULT_ON_MAX_RETRIES
    )
    return WorkflowDefaults(max_retries=max_retries, on_max_retries=on_max)


def load_workflow(workflow_dir: Path) -> Workflow:
    """Parse <workflow_dir>/workflow.yaml + agents/*.md into a Workflow.

    Task 5 wires phases/defaults only. Step gates + fs_permissions land in
    Tasks 6-7; validators run in Task 9.
    """
    yaml_path = workflow_dir / "workflow.yaml"
    if not yaml_path.exists():
        raise LoadError(
            f"workflow.yaml not found in {workflow_dir}",
            SourceLoc(file=yaml_path, line=1),
        )
    try:
        raw = yaml.safe_load(yaml_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LoadError(
            f"invalid YAML: {e}", SourceLoc(file=yaml_path, line=1)
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(
            "workflow.yaml root must be a mapping",
            SourceLoc(file=yaml_path, line=1),
        )

    root_source = SourceLoc(file=yaml_path, line=1)
    if "phases" not in raw:
        raise LoadError("missing 'phases' list", root_source)
    if not isinstance(raw["phases"], list) or not raw["phases"]:
        raise LoadError("'phases' must be a non-empty list", root_source)

    defaults = _parse_defaults(raw.get("defaults"), root_source)

    # Pre-parse agent files in agents/ so steps can reference by relative path.
    agents: dict[str, AgentDef] = {}
    agents_dir = workflow_dir / "agents"
    if agents_dir.is_dir():
        for md in sorted(agents_dir.glob("*.md")):
            agent = parse_agent_file(md)
            if agent.name in agents:
                raise LoadError(
                    f"duplicate agent name {agent.name!r} "
                    f"(also declared in {agents[agent.name].source_path})",
                    agent.source,
                )
            agents[agent.name] = agent

    seen_phases: set[str] = set()
    phase_objs: list[Phase] = []
    for pname in raw["phases"]:
        if pname is None:
            raise LoadError("phases list contains a null entry", root_source)
        pname = str(pname)
        if pname in seen_phases:
            raise LoadError(f"duplicate phase {pname!r} in phases: list", root_source)
        seen_phases.add(pname)
        if pname not in raw:
            raise LoadError(
                f"phase {pname!r} listed in phases: but no '{pname}:' section",
                root_source,
            )
        phase_objs.append(
            _parse_phase(pname, raw[pname], workflow_dir, defaults, yaml_path)
        )

    return Workflow(
        name=str(raw.get("name", workflow_dir.name)),
        description=str(raw.get("description", "")),
        phases=tuple(phase_objs),
        defaults=defaults,
        agents=agents,
        workflow_dir=workflow_dir,
        source=root_source,
    )


def _parse_phase(
    name: str,
    raw: dict[str, Any],
    workflow_dir: Path,
    defaults: WorkflowDefaults,
    yaml_path: Path,
) -> Phase:
    """Task 5 stub: builds an empty-gates phase. Steps + gates filled in Tasks 6-7."""
    source = SourceLoc(file=yaml_path, line=1)
    if not isinstance(raw, dict):
        raise LoadError(f"phase {name!r} must be a mapping", source)
    if "steps" not in raw:
        raise LoadError(f"phase {name!r} missing 'steps'", source)

    steps_raw = raw["steps"]
    if not isinstance(steps_raw, list):
        raise LoadError(
            f"phase {name!r}: 'steps' must be a list, got {type(steps_raw).__name__}",
            source,
        )
    steps = tuple(
        _parse_step_minimal(s, workflow_dir, defaults, yaml_path) for s in steps_raw
    )
    return Phase(
        name=name,
        steps=steps,
        gates=(),  # Task 7 wires phase-level gates
        max_retries=_resolve_max_retries(raw, defaults.max_retries, source, f"phase {name!r}"),
        on_max_retries=(
            _parse_action(raw["on_max_retries"], source)
            if "on_max_retries" in raw
            else defaults.on_max_retries
        ),
        source=source,
    )


def _parse_step_minimal(
    raw: dict[str, Any],
    workflow_dir: Path,
    defaults: WorkflowDefaults,
    yaml_path: Path,
) -> Step:
    """Task 5 stub: name + file only. Gates + fs_permissions land in Task 6."""
    source = SourceLoc(file=yaml_path, line=1)
    if not isinstance(raw, dict):
        raise LoadError(f"step must be a mapping, got {type(raw).__name__}", source)
    if "name" not in raw or raw["name"] is None:
        raise LoadError("step missing 'name'", source)
    if not isinstance(raw["name"], str):
        raise LoadError(
            f"step 'name' must be a string, got {type(raw['name']).__name__}",
            source,
        )
    if "file" not in raw or raw["file"] is None:
        raise LoadError(f"step {raw['name']!r} missing 'file'", source)
    if not isinstance(raw["file"], str):
        raise LoadError(
            f"step {raw['name']!r}: 'file' must be a string", source,
        )
    model_role_raw = raw.get("model_role")
    model_role = "default" if model_role_raw is None else str(model_role_raw)
    return Step(
        name=raw["name"],
        file=workflow_dir / raw["file"],
        interactive=bool(raw.get("interactive", False)),
        model_role=model_role,
        fs_permissions=(),
        gates=(),
        max_retries=_resolve_max_retries(
            raw, defaults.max_retries, source, f"step {raw['name']!r}"
        ),
        on_max_retries=(
            _parse_action(raw["on_max_retries"], source)
            if "on_max_retries" in raw
            else defaults.on_max_retries
        ),
        source=source,
    )
