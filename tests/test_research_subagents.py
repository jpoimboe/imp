from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _wf():
    return load_workflow(bundled_workflow_dir("imp"))


def test_explorer_agent_is_read_only_leaf() -> None:
    a = _wf().agents["explorer"]
    assert a.name == "explorer"
    tools = set(a.tools)
    assert tools == {"read_file", "glob", "grep", "bash", "query_events"}
    for forbidden in ("write_file", "edit_file", "web_search", "web_fetch",
                      "task", "wait_for_user"):
        assert forbidden not in tools
    assert a.subagents == ()
    fields = {f.name for f in a.output_schema.fields}
    assert {"status", "report"} <= fields


def test_researcher_agent_has_web_tools_read_only() -> None:
    a = _wf().agents["researcher"]
    assert set(a.tools) == {"read_file", "glob", "grep", "bash",
                             "query_events", "web_search", "web_fetch"}
    assert a.subagents == ()
    fields = {f.name for f in a.output_schema.fields}
    assert {"status", "report"} <= fields


def test_subagents_have_no_static_tier() -> None:
    agents_dir = bundled_workflow_dir("imp") / "agents"
    for name in ("explorer", "researcher"):
        text = (agents_dir / f"{name}.md").read_text()
        # No frontmatter tier: each inherits the dispatching agent's model.
        assert "\ntier:" not in text


def test_plan_writer_declares_research_subagents() -> None:
    a = _wf().agents["plan-writer"]
    assert "task" in a.tools
    assert set(a.subagents) == {"explorer", "researcher"}


def test_spec_writer_declares_research_subagents() -> None:
    a = _wf().agents["spec-finalizer"]
    assert "task" in a.tools
    assert set(a.subagents) == {"explorer", "researcher"}


def test_spec_writer_prose_relaxes_throttle_and_adds_research(tmp_path) -> None:
    body = _wf().agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "no more than 3 question rounds" not in out
    assert "Do not over-research a clear request" not in out
    assert "Research before you converge" in out
    assert 'task(subagent_name="explorer"' in out
    assert "researcher" in out
    assert "no cost gating" in out


def test_spec_writer_design_quality_and_coverage_guidance(tmp_path) -> None:
    body = _wf().agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "present it in sections" in out
    assert "do not propose unrelated refactoring" in out
    assert "architecture, components, data flow, error handling, and testing" in out
