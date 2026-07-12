"""Resolved project memory is injected by build_agent_invocation into an agent's
body under a `## Project memory` section: a top-level agent gets its
workflow/step slice broad->narrow; a subagent gets only global all.md; empty
memory emits no section (spec hede92 AC 1-4)."""
from __future__ import annotations

from pathlib import Path

from imp.agents.runner import Message, QuestContext, TextBlock
from imp.engine.agent_run import build_agent_invocation
from tests.test_agent_run import _agent

_HEAD = "## Project memory"
_MEM = {
    "all.md": "GLOBAL MEMO",
    "imp/all.md": "WORKFLOW MEMO",
    "imp/s.md": "STEP MEMO",
}


def _cc(tmp: Path, memory: dict[str, str]) -> QuestContext:
    return QuestContext(
        title="t", project_root=tmp, workflow_dir=tmp / "wf",
        memory=memory, workflow_name="imp", step_name="s")


def _inv(tmp: Path, *, memory: dict[str, str], subagent: bool, feedback_lead=None):
    return build_agent_invocation(
        agent_def=_agent(),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(
            (Message(role="user", content=(TextBlock(text="SUB TASK"),)),)
            if subagent else ()),
        quest_context=_cc(tmp, memory),
        user_chat_input_fn=None, transcript_dir=tmp / "td",
        events_path=tmp / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        quest_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None,
        subagent_depth=1 if subagent else 0,
        feedback_lead=feedback_lead,
    )


def test_toplevel_injects_memory_broad_to_narrow(tmp_path: Path) -> None:
    inv = _inv(tmp_path, memory=_MEM, subagent=False)
    text = inv.initial_messages[0].content[0].text
    assert _HEAD in text
    assert "follow it" in text  # the maintainer lead-in rides along
    assert text.index("GLOBAL MEMO") < text.index("WORKFLOW MEMO") < text.index("STEP MEMO")
    assert text.index(_HEAD) < text.index("RENDERED BODY")
    assert _HEAD not in inv.system_prompt  # not the role-only system prompt


def test_subagent_gets_only_global_memory(tmp_path: Path) -> None:
    inv = _inv(tmp_path, memory=_MEM, subagent=True)
    sp = inv.system_prompt
    assert "GLOBAL MEMO" in sp
    assert "WORKFLOW MEMO" not in sp  # subagents have no workflow/step identity
    assert "STEP MEMO" not in sp
    assert sp.index(_HEAD) < sp.index("RENDERED BODY")


def test_empty_memory_injects_no_section(tmp_path: Path) -> None:
    inv = _inv(tmp_path, memory={}, subagent=False)
    text = inv.initial_messages[0].content[0].text
    assert "Project memory" not in text
    assert text.startswith("RENDERED BODY")


def test_feedback_lead_leads_ahead_of_memory_section(tmp_path: Path) -> None:
    inv = _inv(tmp_path, memory=_MEM, subagent=False,
               feedback_lead="Here's the feedback to address: fix X")
    text = inv.initial_messages[0].content[0].text
    assert text.startswith("Here's the feedback to address: fix X")
    assert text.index("Here's the feedback") < text.index(_HEAD)
    assert text.index(_HEAD) < text.index("RENDERED BODY")


def test_no_workflow_template_references_project_guidance() -> None:
    roots = [
        Path("src/imp/assets/workflows/imp"),
        Path("src/imp/assets/workflows/patent"),
    ]
    offenders = [
        str(p) for root in roots for p in root.rglob("*.md")
        if "project.guidance" in p.read_text()
    ]
    assert offenders == [], offenders
