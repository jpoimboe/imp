from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.agents.reflow import reflow_markdown
from imp.agents.runner import Message, QuestContext, TextBlock
from imp.agents.subagents import dispatch_task
from imp.agents.tools.context import ToolContext
from imp.assets import bundled_workflow_dir
from imp.cli.signals import InterruptCoordinator
from imp.engine.agent_run import (
    _CORE_HEAD,
    _CORE_TAIL,
    SUBAGENT_DELIVERABLE_NOTE,
    build_agent_invocation,
)
from imp.engine.executor import RunResult, run_quest
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from tests.test_executor import _register_invocation_capturing
from tests.test_executor_persona import _seed
from tests.test_subagents import _agent as _subagent_def
from tests.test_subagents import _spawn_yielding

_PERSONA_FILE = Path("src/imp/assets/workflows/default/persona.md")

_PERSONA_TEXT = """# The verify gates own the full suite

Never run the project's full test suite, lint, or typecheck. The `verify`,
`converge-verify` and `integration-verify` steps are mechanical gates that run the
project's combined checks and route failures back; a run of your own only duplicates
them, at real cost on a machine carrying other work. Run only the narrowest check
that tests a specific hypothesis you actually hold.
"""

# what build_agent_invocation splices in: the file's text with its trailing newline gone
_PERSONA_BODY = _PERSONA_TEXT.strip("\n")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent() -> AgentDef:
    return AgentDef(
        name="a", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="body", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1), max_tool_calls=None)


def _rendered_persona() -> str:
    wf_dir = bundled_workflow_dir("default")
    return render_prompt(
        load_workflow(wf_dir).persona_source,
        build_context(quest_title="t", event_log=InMemoryEventLog()),
        workflow_dir=wf_dir)


def _build(tmp_path: Path, *, subagent_depth: int):
    task = (Message(role="user", content=(TextBlock(text="TASK"),)),)
    return build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="STEP BODY", rendered_persona=_rendered_persona(),
        initial_messages=task if subagent_depth else (),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                   workflow_dir=bundled_workflow_dir("default")),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="step:p/s", event_log=None,
        quest_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None, subagent_depth=subagent_depth)


def _head_persona_tail() -> str:
    return reflow_markdown(f"{_CORE_HEAD}\n\n{_PERSONA_BODY}\n\n{_CORE_TAIL}")


def test_default_workflow_ships_the_verify_gate_persona() -> None:
    assert _PERSONA_FILE.read_text() == _PERSONA_TEXT
    assert load_workflow(bundled_workflow_dir("default")).persona_source == _PERSONA_TEXT


def test_persona_lands_between_head_and_tail_for_an_agent_step(tmp_path: Path) -> None:
    inv = _build(tmp_path, subagent_depth=0)
    assert inv.system_prompt.startswith(_head_persona_tail())
    # the step body rides the first user turn, after the persona
    assert "STEP BODY" in inv.initial_messages[0].content[0].text


def test_persona_lands_between_head_and_tail_for_a_subagent(tmp_path: Path) -> None:
    sp = _build(tmp_path, subagent_depth=1).system_prompt
    assert sp.startswith(_head_persona_tail())
    assert sp.index("Never run the project's full test suite") < sp.index("STEP BODY")
    assert sp.index("STEP BODY") < sp.index(SUBAGENT_DELIVERABLE_NOTE)


def test_dispatched_subagent_receives_the_shipped_persona(tmp_path: Path) -> None:
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"explorer": _subagent_def("explorer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.quest_context = QuestContext(
        id="q1", title="Q", project_root=tmp_path,
        workflow_dir=bundled_workflow_dir("default"),
        persona_source=load_workflow(bundled_workflow_dir("default")).persona_source)
    capture: dict = {}
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}], capture=capture)

    assert anyio.run(dispatch_task, ctx, "explorer", "map the code") == "ok"
    sp = capture["inv"].system_prompt
    assert sp.startswith(_head_persona_tail())
    # the dispatched body ("hi {{ quest.id }}") renders after the persona
    assert sp.index("Never run the project's full test suite") < sp.index("hi q1")


@pytest.mark.anyio
async def test_executor_hands_the_persona_source_to_the_quest_context(
    tmp_path: Path,
) -> None:
    quest = _seed(tmp_path, persona=_PERSONA_FILE.read_text())
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)
    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED
    # the RAW source rides the QuestContext; subagents.py re-renders it per dispatch
    assert sink[0].quest_context.persona_source == _PERSONA_TEXT


def _agent_body(name: str) -> str:
    wf = load_workflow(bundled_workflow_dir("default"))
    return wf.agents[name].prompt_template_source


def test_spiker_carve_out_overrides_the_persona() -> None:
    body = _agent_body("spiker")
    assert "**The full-suite rule does not bind you here.**" in body
    assert "a full suite run" in body
    # the carve-out closes the disposable-worktree section it qualifies
    assert body.index("## Your worktree is disposable") < body.index(
        "**The full-suite rule does not bind you here.**")
    assert body.index("**The full-suite rule does not bind you here.**") < body.index(
        "## Investigate empirically")


def test_action_care_partial_narrows_to_focused_tests() -> None:
    text = Path("src/imp/assets/partials/action-care.md").read_text()
    assert "(edit files, run focused tests)" in text
    assert "(edit files, run tests)" not in text


def test_finalize_reports_the_gate_verdict_instead_of_running_tests() -> None:
    body = _agent_body("finalize")
    assert "only if cheap" not in body
    assert "make NO code changes and run no tests." in body
    assert "report that outcome in `verify_status`" in body
    assert "**You are not a reviewer.**" in body


def test_no_stale_no_persona_claim() -> None:
    engine = Path("src/imp/engine/agent_run.py").read_text()
    golden = Path("tests/test_persona_byte_identity.py").read_text()
    assert "imp itself ships none" not in engine
    # the claim is corrected, not merely deleted; short anchor so re-wording is free
    assert "the default workflow ships one" in engine
    assert "imp ships no persona" not in golden
