from __future__ import annotations

import re
from pathlib import Path

from imp.agents.reflow import reflow_markdown
from imp.agents.runner import ChangeContext
from imp.engine.agent_run import _CORE_HEAD, _CORE_TAIL, build_agent_invocation
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

_PERSONA = Path("src/imp/assets/workflows/imp/persona.md")


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
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None, resolved_tiers=None,
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


def test_persona_carries_the_domain_language() -> None:
    # Acceptance #2: the moved text now lives only in the imp persona.
    persona = _PERSONA.read_text().lower()
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
    built = CodexAgentRunner._build_prompt(None, inv.system_prompt)
    assert built.startswith(inv.system_prompt)
    assert reflow_markdown(_CORE_HEAD) in built
    assert reflow_markdown(_CORE_TAIL) in built


def test_base_has_numbered_options_convention() -> None:
    low = _CORE_TAIL.lower()
    assert "numbered list" in low
    assert "single digit" in low
    for banned in ("go live", "go-live", "build/park", "build now or save"):
        assert banned not in low, banned


def test_base_has_recommendation_anchored_and_no_jargon_conventions() -> None:
    low = _CORE_TAIL.lower()
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
