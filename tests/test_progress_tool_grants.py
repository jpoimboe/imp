from __future__ import annotations

from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.errors import LoadError
from imp.engine.loader import (
    ALLOWABLE_TOOLS,
    BASE_CAPS,
    _effective_tools,
    load_workflow,
    parse_agent_file,
)
from imp.engine.types import AgentDef, OutputSchema, SourceLoc


def _write_agent_md(path: Path, frontmatter: str, body: str = "prompt body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return path


def test_update_step_label_is_opt_in() -> None:
    assert "update_step_label" not in BASE_CAPS
    assert "update_step_label" in ALLOWABLE_TOOLS


def test_task_lead_receives_progress_tools() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    assert "update_step_label" in wf.agents["task-lead"].tools


def test_agent_without_allow_tools_lacks_update_step_label() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    assert "update_step_label" not in wf.agents["threshold"].tools


def test_allow_tools_grants_update_step_label(tmp_path: Path) -> None:
    p = _write_agent_md(
        tmp_path / "x.md",
        frontmatter="name: x\nallow_tools: [update_step_label]",
    )
    agent = parse_agent_file(p)
    assert "update_step_label" in agent.allow_tools
    assert agent.allow_tools == frozenset({"update_step_label"})


def test_effective_tools_surfaces_update_step_label() -> None:
    fake = SourceLoc(file=Path("/fake"), line=1)
    schema = OutputSchema(fields=(), source=fake)
    agent_with = AgentDef(
        name="x", description="", tools=frozenset(), subagents=(),
        output_schema=schema, prompt_template_source="", source_path=Path("/fake"),
        source=fake, allow_tools=frozenset({"update_step_label"}),
    )
    agent_without = AgentDef(
        name="y", description="", tools=frozenset(), subagents=(),
        output_schema=schema, prompt_template_source="", source_path=Path("/fake"),
        source=fake,
    )
    assert "update_step_label" in _effective_tools(agent_with, is_sub=False)
    assert "update_step_label" not in _effective_tools(agent_without, is_sub=False)


def test_allowable_tools_disjoint_from_base_caps() -> None:
    assert ALLOWABLE_TOOLS.isdisjoint(BASE_CAPS)


def test_allow_tools_unknown_tool_kept_not_fatal(tmp_path: Path) -> None:
    """An unknown allow entry no longer raises at parse time: it is kept verbatim
    (its unknown-ness becomes a non-fatal load warning)."""
    p = _write_agent_md(
        tmp_path / "x.md",
        frontmatter="name: x\nallow_tools: [bogus_tool]",
    )
    agent = parse_agent_file(p)
    assert agent.allow_tools == frozenset({"bogus_tool"})


def test_tools_and_allow_tools_conflict_raises(tmp_path: Path) -> None:
    p = _write_agent_md(
        tmp_path / "x.md",
        frontmatter="name: x\ntools: [read_file]\nallow_tools: [update_step_label]",
    )
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "not both" in str(ei.value)


def test_allow_tools_not_list_raises(tmp_path: Path) -> None:
    p = _write_agent_md(
        tmp_path / "x.md",
        frontmatter="name: x\nallow_tools: update_step_label",
    )
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "allow_tools" in str(ei.value)
