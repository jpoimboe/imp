from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import ChangeContext
from imp.engine.agent_run import build_agent_invocation
from imp.engine.state import JsonlEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

_ASSET = Path("src/imp/assets/workflows/imp")
_GOLDEN = Path("tests/golden")


def _agent(tools):
    return AgentDef(
        name="rev", description="", tools=frozenset(tools), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="body", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1), max_tool_calls=None)


def _rendered_imp_persona(tmp_path: Path) -> str:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_title="t", event_log=log)
    return render_prompt((_ASSET / "persona.md").read_text(), ctx,
                         workflow_dir=_ASSET)


def _build(tmp_path, tools):
    return build_agent_invocation(
        agent_def=_agent(tools), provider_name="anthropic", model_name="m",
        rendered_system_prompt="STEP BODY",
        rendered_persona=_rendered_imp_persona(tmp_path), initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=_ASSET),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None).system_prompt


@pytest.mark.parametrize("tools,golden", [
    (frozenset({"complete_step"}), "imp_base_prompt_nobash.txt"),
    (frozenset({"complete_step", "bash"}), "imp_base_prompt_bash.txt"),
])
def test_imp_assembled_prompt_byte_identical_after_split(tmp_path, tools, golden):
    expected = (_GOLDEN / golden).read_text()
    assert _build(tmp_path, tools) == expected
