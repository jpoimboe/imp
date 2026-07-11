"""Project guidance is injected by the engine (build_agent_invocation) into an
agent's body when the change froze non-empty guidance, and produces no
scaffolding when unset (vima66 §1). Templates no longer carry a
{{ project.guidance }} block (see the guard test below)."""
from __future__ import annotations

from pathlib import Path

from imp.agents.runner import Message, QuestContext, TextBlock
from imp.engine.agent_run import build_agent_invocation
from tests.test_agent_run import _agent

_HEAD = "## Project guidance"


def _cc(tmp: Path, guidance: str) -> QuestContext:
    return QuestContext(title="t", project_root=tmp,
                         workflow_dir=tmp / "wf", guidance=guidance)


def _inv(tmp: Path, *, guidance: str, subagent: bool, feedback_lead=None):
    return build_agent_invocation(
        agent_def=_agent(),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(
            (Message(role="user", content=(TextBlock(text="SUB TASK"),)),)
            if subagent else ()),
        quest_context=_cc(tmp, guidance),
        user_chat_input_fn=None, transcript_dir=tmp / "td",
        events_path=tmp / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None,
        subagent_depth=1 if subagent else 0,
        feedback_lead=feedback_lead,
    )


def test_toplevel_injects_guidance_at_head_of_user_task(tmp_path: Path) -> None:
    inv = _inv(tmp_path, guidance="NO BACKCOMPAT.", subagent=False)
    text = inv.initial_messages[0].content[0].text
    assert _HEAD in text
    assert "NO BACKCOMPAT." in text
    assert "follow it" in text                       # the maintainer sentence rides along
    assert text.index(_HEAD) < text.index("RENDERED BODY")
    assert _HEAD not in inv.system_prompt             # not the role-only system prompt


def test_subagent_injects_guidance_at_head_of_body(tmp_path: Path) -> None:
    inv = _inv(tmp_path, guidance="NO BACKCOMPAT.", subagent=True)
    sp = inv.system_prompt
    assert _HEAD in sp
    assert "NO BACKCOMPAT." in sp
    assert sp.index(_HEAD) < sp.index("RENDERED BODY")


def test_empty_guidance_injects_no_section_no_scaffolding(tmp_path: Path) -> None:
    inv = _inv(tmp_path, guidance="", subagent=False)
    text = inv.initial_messages[0].content[0].text
    assert "Project guidance" not in text
    assert text.startswith("RENDERED BODY")          # body head untouched


def test_feedback_lead_leads_ahead_of_guidance_section(tmp_path: Path) -> None:
    inv = _inv(tmp_path, guidance="NO BACKCOMPAT.", subagent=False,
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
