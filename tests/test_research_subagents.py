from __future__ import annotations

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow


def _wf():
    return load_workflow(default_workflow_dir())


def test_explorer_agent_is_read_only_leaf() -> None:
    a = _wf().agents["explorer"]
    assert a.name == "explorer"
    tools = set(a.tools)
    assert tools == {"complete_step", "read_file", "glob", "grep", "bash",
                     "query_events"}
    for forbidden in ("write_file", "edit_file", "web_search", "web_fetch",
                      "task", "wait_for_user"):
        assert forbidden not in tools
    assert a.subagents == ()
    fields = {f.name for f in a.output_schema.fields}
    assert {"status", "report"} <= fields


def test_researcher_agent_has_web_tools_read_only() -> None:
    a = _wf().agents["researcher"]
    assert set(a.tools) == {"complete_step", "read_file", "glob", "grep", "bash",
                             "query_events", "web_search", "web_fetch"}
    assert a.subagents == ()
    fields = {f.name for f in a.output_schema.fields}
    assert {"status", "report"} <= fields


def test_subagents_have_no_static_tier() -> None:
    agents_dir = default_workflow_dir() / "agents"
    for name in ("explorer", "researcher"):
        text = (agents_dir / f"{name}.md").read_text()
        # No frontmatter tier: each inherits the dispatching agent's model.
        assert "\ntier:" not in text


def test_plan_writer_declares_research_subagents() -> None:
    a = _wf().agents["plan-writer"]
    assert "task" in a.tools
    assert set(a.subagents) == {"explorer", "researcher"}


def test_spec_writer_declares_research_subagents() -> None:
    a = _wf().agents["spec-writer"]
    assert "task" in a.tools
    assert set(a.subagents) == {"explorer", "researcher"}


def test_spec_writer_prose_relaxes_throttle_and_adds_research() -> None:
    body = _wf().agents["spec-writer"].prompt_template_source
    assert "no more than 3 question rounds" not in body
    assert "Do not over-research a clear request" not in body
    assert "Research before you converge" in body
    assert 'task(subagent_name="explorer"' in body
    assert "researcher" in body
    assert "no cost gating" in body


def test_spec_writer_design_quality_and_coverage_guidance() -> None:
    body = _wf().agents["spec-writer"].prompt_template_source
    assert "present it in sections" in body
    assert "do not propose unrelated refactoring" in body
    assert "architecture, components, data flow, error handling, and testing" in body
