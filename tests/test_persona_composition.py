from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import ChangeContext
from imp.engine.agent_run import _CORE_HEAD, _CORE_TAIL, build_agent_invocation
from imp.engine.state import JsonlEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


def _agent(tools=frozenset({"complete_step"})):
    return AgentDef(
        name="a", description="", tools=frozenset(tools), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="body", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1), max_tool_calls=None)


def _build(tmp_path: Path, *, rendered_persona: str):
    return build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="STEP BODY", rendered_persona=rendered_persona,
        initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None)


def test_empty_persona_composes_head_then_tail_with_nothing_between(tmp_path):
    # AC#3: a personaless workflow renders core_head + core_tail, nothing between.
    sp = _build(tmp_path, rendered_persona="").system_prompt
    assert sp.startswith(f"{_CORE_HEAD}\n\n{_CORE_TAIL}")


def test_persona_injected_between_head_and_tail(tmp_path):
    # AC#4: rendered persona lands between head and tail, before the step body.
    sp = _build(tmp_path, rendered_persona="PERSONA TEXT").system_prompt
    assert sp.startswith(f"{_CORE_HEAD}\n\nPERSONA TEXT\n\n{_CORE_TAIL}")
    assert sp.index("PERSONA TEXT") < sp.index("STEP BODY")


def test_persona_renders_change_and_vars_namespaces(tmp_path):
    # AC#4: persona body may reference the Jinja namespaces.
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_title="My Change", event_log=log,
                        vars={"point_of_novelty": "a widget"})
    rendered = render_prompt(
        "Title {{ change.title }}; novelty {{ vars.point_of_novelty }}", ctx)
    assert rendered == "Title My Change; novelty a widget"
    sp = _build(tmp_path, rendered_persona=rendered).system_prompt
    assert "Title My Change; novelty a widget" in sp


def test_persona_can_include_partials(tmp_path):
    # AC#4: persona can {% include "partials/..." %} via FileSystemLoader.
    wf = tmp_path / "wf"
    (wf / "partials").mkdir(parents=True)
    (wf / "partials" / "frag.md").write_text("SHARED FRAGMENT")
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_title="t", event_log=log)
    rendered = render_prompt('{% include "partials/frag.md" %}', ctx,
                             workflow_dir=wf)
    assert rendered == "SHARED FRAGMENT"
