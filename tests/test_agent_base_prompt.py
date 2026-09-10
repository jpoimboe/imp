from __future__ import annotations

import re
from pathlib import Path

from imp.agents.reflow import reflow_markdown
from imp.agents.runner import QuestContext
from imp.engine.agent_run import (
    _CORE_HEAD,
    _CORE_TAIL,
    _USER_FACING_NOTE,
    build_agent_invocation,
)
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

_CODING_STANCE = Path("src/imp/assets/partials/coding-stance.md")
_RENDER_BULLET = (
    "- Rendered something for the user to look at? Paste it into your prose, in a "
    "fenced block, in the same message — tool output never reaches them. If it's a "
    "file they must open (an image, an exported artifact), state its path yourself. "
    "Before ending a turn, check that every \"here's…\" has its thing on screen."
)


def _agent(name: str = "rev", tools=frozenset({"complete_step"})) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(tools), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="body", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1), max_tool_calls=None)


def _build(tmp_path: Path, *, name: str, body: str):
    return build_agent_invocation(
        agent_def=_agent(name=name), provider_name="anthropic", model_name="m",
        rendered_system_prompt=body, initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="step:p/s", event_log=None,
        quest_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None)


def test_base_prefix_byte_identical_across_agents(tmp_path: Path) -> None:
    # With no workflow persona, the engine base is core_head + core_tail; it is
    # byte-identical across agent defs (fails if the base interpolates name/body).
    a = _build(tmp_path, name="alpha", body="BODY ALPHA")
    b = _build(tmp_path, name="omega", body="BODY OMEGA")
    engine_base = reflow_markdown(f"{_CORE_HEAD}\n\n{_CORE_TAIL}")
    assert a.system_prompt.startswith(engine_base)
    assert b.system_prompt.startswith(engine_base)


def test_engine_base_has_no_domain_language() -> None:
    # Acceptance #2: no software-engineering / coding-hygiene language remains in
    # the engine base after the split.
    base = (_CORE_HEAD + _CORE_TAIL).lower()
    for banned in (
        "software engineering",
        "owasp",
        "snake case",
        "premature abstraction",
        "backwards-compatibility",
        "# doing tasks",
    ):
        assert banned not in base, banned


def test_coding_stance_carries_the_domain_language() -> None:
    # Acceptance #2: the moved text now lives only in the imp persona.
    persona = _CODING_STANCE.read_text().lower()
    assert "# doing tasks" in persona
    assert "software engineering" in persona
    assert "owasp" in persona


def test_base_asserts_no_mechanism_imp_lacks() -> None:
    low = (_CORE_HEAD + _CORE_TAIL).lower()
    for banned in (
        "system-reminder", "permission mode", "permission-mode",
        "auto-compact", "compacted", "tool results may be cleared",
        "automatically compressed", "subagent_type", "you are claude code",
    ):
        assert banned not in low, banned
    assert "{{" not in (_CORE_HEAD + _CORE_TAIL)
    assert re.search(r"<[A-Za-z][A-Za-z0-9_]*>", _CORE_HEAD + _CORE_TAIL) is None


def test_base_uses_imp_identity_and_tool_names() -> None:
    assert _CORE_HEAD.startswith("You are imp")
    base = _CORE_HEAD + _CORE_TAIL
    for tool in ("read_file", "edit_file", "write_file", "grep", "glob",
                 "load_skill"):
        assert tool in base, tool


def test_base_reaches_verbatim_provider_field(tmp_path: Path) -> None:
    inv = _build(tmp_path, name="rev", body="BODY")
    assert reflow_markdown(_CORE_HEAD) in inv.system_prompt
    assert reflow_markdown(_CORE_TAIL) in inv.system_prompt


def test_base_survives_codex_prompt_wrap(tmp_path: Path) -> None:
    from imp.providers.codex_runner import CodexAgentRunner
    inv = _build(tmp_path, name="rev", body="BODY")
    built = CodexAgentRunner._build_prompt(None, inv, InMemoryEventLog(), "step:t")
    assert built.startswith(inv.system_prompt)
    assert reflow_markdown(_CORE_HEAD) in built
    assert reflow_markdown(_CORE_TAIL) in built


def test_base_has_numbered_options_convention() -> None:
    low = _USER_FACING_NOTE.lower()
    assert "numbered list" in low
    assert "single digit" in low
    for banned in ("go live", "go-live", "build/park", "build now or save"):
        assert banned not in low, banned


def test_core_tail_emphasizes_parallel_tool_batching() -> None:
    # Spec Part A: the multi-tool bullet must restore the "maximize parallel
    # tool calls" emphasis and the concrete "read several known files in one
    # batch" cue, while still scoping sequencing to genuine dependencies.
    assert "maximize parallel tool calls" in _CORE_TAIL
    assert "read them in one batch" in _CORE_TAIL
    assert "a later call genuinely depends" in _CORE_TAIL


def test_no_comments_rule_merged_into_coding_stance_and_deduped_from_core() -> None:
    # AC 4: the single no-comments rule lives in the persona and carries BOTH the
    # WHY-only exception criteria AND the docstring/one-line ceiling; the core
    # tail no longer restates it, the emoji rule, or brevity.
    persona = _CODING_STANCE.read_text()
    # WHY-only exception criteria retained:
    assert "Only add one when the WHY is non-obvious" in persona
    # docstring / one-line ceiling folded into the SAME rule:
    assert "multi-paragraph docstrings" in persona
    assert "one short line max" in persona
    # duplicated no-comments statement removed from the core tail:
    assert "default to writing no comments" not in _CORE_TAIL
    assert "multi-paragraph docstrings" not in _CORE_TAIL
    # emoji rule stated once (the redundant second sentence is gone):
    assert "Only use emojis if the user explicitly requests it" in _USER_FACING_NOTE
    assert "Avoid using emojis in all communication" not in _CORE_TAIL
    # brevity stated once (the redundant restatements are gone):
    assert "Your responses should be short and concise" not in _CORE_TAIL
    assert "Match responses to the task" not in _CORE_TAIL


def test_base_has_recommendation_anchored_and_no_jargon_conventions() -> None:
    low = _USER_FACING_NOTE.lower()
    assert "plain affirmative" in low
    assert "declining is always implicitly available" in low
    assert "follow-on to the question" in low
    assert "complexity classes" in low
    assert "phase or step ids" in low
    assert "workflow variable names" in low
    assert "internal tool names" in low
    assert "describe the effect in plain language" in low
    assert low.index("numbered list") < low.index("plain affirmative")
    assert low.index("narrate your internal deliberation") < low.index(
        "complexity classes")


def test_user_facing_note_demands_the_render_land_in_the_message() -> None:
    # The old bullet covered only files the user must open; a mockup's rendered
    # bytes are the deliverable, so they have to land in the message itself.
    flat = " ".join(_USER_FACING_NOTE.split())
    assert _RENDER_BULLET in flat
    assert "can't see inline" not in flat
    # Still the third bullet of Tone and style, ahead of the colon/lead-in rule.
    assert flat.index("- Reference code as file_path:line_number") < flat.index(_RENDER_BULLET)
    assert flat.index(_RENDER_BULLET) < flat.index("- Never put a colon before a tool call")


def test_core_tail_does_not_contain_users_see_only() -> None:
    assert "Users see only your text output" not in _CORE_TAIL


def test_user_facing_note_contains_top_level_output_contract() -> None:
    flat = " ".join(_USER_FACING_NOTE.split())
    assert (
        "Users see only your text output, not tool calls or thinking. "
        "Before your first tool call, state in one sentence what you're about to do."
    ) in flat
    assert "give short updates at key moments" not in flat
    assert "Brief is good — silent is not." not in flat
    assert (
        "Don't narrate your internal deliberation. User-facing text is relevant "
        "communication, not commentary — state results and decisions directly."
    ) in flat


def test_top_level_invocation_contains_user_facing_note(tmp_path: Path) -> None:
    inv = _build(tmp_path, name="rev", body="BODY")
    assert "Users see only your text output" in inv.system_prompt


def test_subagent_invocation_omits_user_facing_note(tmp_path: Path) -> None:
    from imp.agents.runner import Message, TextBlock
    task = (Message(role="user", content=(TextBlock(text="SUBAGENT TASK"),)),)
    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=task,
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                   workflow_dir=tmp_path / "wf"),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="sub:x", event_log=None,
        quest_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None, subagent_depth=1)
    assert "Users see only your text output" not in inv.system_prompt
    all_msgs = "".join(
        b.text
        for m in inv.initial_messages
        for b in m.content
        if isinstance(b, TextBlock)
    )
    assert "Users see only your text output" not in all_msgs


def test_both_agents_have_using_your_tools(tmp_path: Path) -> None:
    from imp.agents.runner import Message, TextBlock
    top = _build(tmp_path, name="top", body="BODY")
    assert "maximize parallel tool calls" in top.system_prompt

    task = (Message(role="user", content=(TextBlock(text="SUBAGENT TASK"),)),)
    sub = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=task,
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                   workflow_dir=tmp_path / "wf"),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="sub:x", event_log=None,
        quest_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None, subagent_depth=1)
    assert "maximize parallel tool calls" in sub.system_prompt
