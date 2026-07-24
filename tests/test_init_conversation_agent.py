from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import parse_agent_file
from imp.engine.types import AgentDef


def _agent() -> AgentDef:
    return parse_agent_file(
        bundled_workflow_dir("default") / "agents" / "init-conversation.md")


def test_init_conversation_is_read_only() -> None:
    a = _agent()
    assert a.name == "init-conversation"
    assert set(a.tools) == {"read_file", "glob", "grep", "bash", "symbol_outline"}


def test_init_conversation_output_schema() -> None:
    fields = {f.name: f for f in _agent().output_schema.fields}
    assert set(fields) == {
        "status", "git_init", "default_workflow", "default_base",
        "test_cmd_fast", "pythonpath"}
    assert fields["status"].type == "enum"
    assert set(fields["status"].values) == {"Success", "Error"}
    assert fields["git_init"].type == "bool"
    assert fields["default_workflow"].type == "string"
    assert fields["default_base"].type == "string"
    assert fields["test_cmd_fast"].type == "string"
    assert fields["pythonpath"].type == "object"


def test_init_conversation_body_carries_pinned_framing() -> None:
    body = _agent().prompt_template_source
    assert "which way do you want quests to run by default" in body
    assert "**minimal** scopes a small change" in body
    assert "switch per-quest later with --workflow" in body
    assert "import root src. Writing it now." in body
