from __future__ import annotations

from pathlib import Path

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner


def _agent(drivers: frozenset[str]) -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc,
        drivers=drivers)


def _inv(tmp_path: Path, drivers: frozenset[str]) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(drivers), provider_name="claude_code",
        model_name="claude-sonnet-4-6", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(slug="s", title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl")


def test_driver_registers_stdio_server_and_wildcard(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-6")
    opts = runner._build_options(
        _inv(tmp_path, frozenset({"impty"})),
        mcp_server=object(), tool_names=["bash"])
    assert opts.mcp_servers["impty"] == {
        "type": "stdio", "command": "python", "args": ["-m", "impty.mcp"]}
    assert "mcp__impty__*" in opts.allowed_tools
    assert "imp" in opts.mcp_servers


def test_no_driver_no_impty_grant(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-6")
    opts = runner._build_options(
        _inv(tmp_path, frozenset()),
        mcp_server=object(), tool_names=["bash"])
    assert "impty" not in opts.mcp_servers
    assert "mcp__impty__*" not in opts.allowed_tools
