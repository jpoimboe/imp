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
    assert tools == {"read_file", "glob", "grep", "bash", "query_events", "symbol_outline"}
    for forbidden in ("write_file", "edit_file", "web_search", "web_fetch",
                      "sub_agent", "wait_for_user"):
        assert forbidden not in tools
    assert a.subagents == ()
    # A dispatched subagent carries no output schema — it reports in prose.
    assert a.output_schema.fields == ()


def test_researcher_agent_has_web_tools_read_only() -> None:
    a = _wf().agents["researcher"]
    assert set(a.tools) == {"read_file", "glob", "grep", "bash",
                             "query_events", "web_search", "web_fetch"}
    assert a.subagents == ()
    assert a.output_schema.fields == ()


def test_subagents_have_pinned_static_tier() -> None:
    wf = _wf()
    assert wf.agents["explorer"].tier == "medium"
    assert wf.agents["researcher"].tier == "high"
    assert wf.agents["spiker"].tier == "high"


def test_plan_writer_declares_research_subagents() -> None:
    a = _wf().agents["plan-writer"]
    assert "sub_agent" in a.tools
    assert set(a.subagents) == {"explorer", "researcher"}


def test_spec_writer_declares_research_subagents() -> None:
    a = _wf().agents["spec-finalizer"]
    assert "sub_agent" in a.tools
    assert set(a.subagents) == {"explorer", "researcher"}


def test_spec_writer_prose_relaxes_throttle_and_adds_research(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "no more than 3 question rounds" not in out
    assert "Do not over-research a clear request" not in out
    assert "Research before you converge" in out
    assert 'sub_agent(subagent_name="explorer"' in out
    assert "researcher" in out
    assert "no cost gating" in out


def test_delegate_partial_included_in_explore_heavy_steps() -> None:
    wf = _wf()
    for name in ("plan-writer", "spec-finalizer", "task-lead", "finalize"):
        body = wf.agents[name].prompt_template_source
        assert '{% include "partials/delegate-exploration.md" %}' in body, name


def test_task_lead_declares_research_subagents() -> None:
    a = _wf().agents["task-lead"]
    assert set(a.subagents) == {"coder", "code-reviewer", "explorer", "researcher", "spiker"}
    assert "sub_agent" in a.tools


def test_finalize_declares_research_subagents() -> None:
    a = _wf().agents["finalize"]
    assert set(a.subagents) == {"explorer", "researcher"}
    assert "sub_agent" in a.tools


def test_delegate_exploration_partial_exists_and_carries_the_rule() -> None:
    from pathlib import Path
    p = Path("src/imp/assets/workflows/imp/partials/delegate-exploration.md")
    text = p.read_text()
    # The default rule: delegate to explorer/researcher; read inline only what you edit.
    assert "read inline only what you will edit" in text.lower()
    assert 'sub_agent(subagent_name="explorer"' in text
    assert "researcher" in text


def test_brainstorm_dialogue_includes_partial_and_drops_inline_duplication() -> None:
    from pathlib import Path
    body = Path(
        "src/imp/assets/workflows/imp/partials/brainstorm-dialogue.md"
    ).read_text()
    assert '{% include "partials/delegate-exploration.md" %}' in body
    # The extracted delegation specifics no longer live inline in brainstorm-dialogue.
    assert "prefer dispatching\nparallel" not in body
    assert "fan deep mapping out to the subagents" not in body


def test_spec_writer_design_quality_and_coverage_guidance(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "present it in sections" in out
    assert "do not propose unrelated refactoring" not in out
    assert "improvements to the code this change touches." in out
    assert "architecture, components, data flow, error handling, and testing" in out
