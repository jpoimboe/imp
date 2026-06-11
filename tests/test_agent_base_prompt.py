from __future__ import annotations

import re
from pathlib import Path

from imp.agents.runner import ChangeContext
from imp.engine.agent_run import _AGENT_BASE_PROMPT, build_agent_invocation
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


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
        change_context=ChangeContext(slug="s", title="t", project_root=tmp_path,
                                      workflow_dir=tmp_path / "wf"),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl", subagent_defs={},
        tiers=(), max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None, resolved_tiers=None,
        coordinator=None)


def test_base_prefix_byte_identical_across_agents(tmp_path: Path) -> None:
    # Acceptance #4: two different agent defs assembled in the same run share a
    # byte-identical base prefix. Fails if the base ever interpolates the agent
    # name/body (an accidental f-string), because the prefixes would diverge.
    a = _build(tmp_path, name="alpha", body="BODY ALPHA")
    b = _build(tmp_path, name="omega", body="BODY OMEGA")
    n = len(_AGENT_BASE_PROMPT)
    assert a.system_prompt[:n] == _AGENT_BASE_PROMPT
    assert b.system_prompt[:n] == _AGENT_BASE_PROMPT
    assert a.system_prompt[:n] == b.system_prompt[:n]


def test_base_asserts_no_mechanism_imp_lacks() -> None:
    # Acceptance #6: the base claims no capability imp lacks.
    low = _AGENT_BASE_PROMPT.lower()
    for banned in (
        "system-reminder",
        "permission mode",
        "permission-mode",
        "auto-compact",
        "compacted",
        "tool results may be cleared",
        "automatically compressed",
        "subagent_type",
        "you are claude code",
    ):
        assert banned not in low, banned
    # No unfilled template placeholders (Jinja or `<placeholder>`).
    assert "{{" not in _AGENT_BASE_PROMPT
    assert re.search(r"<[A-Za-z][A-Za-z0-9_]*>", _AGENT_BASE_PROMPT) is None


def test_base_uses_imp_identity_and_tool_names() -> None:
    # Acceptance #6: imp's own identity line and imp's tool names.
    assert _AGENT_BASE_PROMPT.startswith("You are imp")
    for tool in ("read_file", "edit_file", "write_file", "grep", "glob",
                 "load_skill"):
        assert tool in _AGENT_BASE_PROMPT, tool
