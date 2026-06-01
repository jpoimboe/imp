from __future__ import annotations

from pathlib import Path

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner


def _agent(tools: frozenset[str]) -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc)


def _inv(tmp_path: Path, tools: frozenset[str]) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(tools), provider_name="claude_code",
        model_name="claude-sonnet-4-6", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(slug="s", title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl")


def test_web_search_declared_enables_builtin(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-6")
    opts = runner._build_options(
        _inv(tmp_path, frozenset({"complete_step", "web_search"})),
        mcp_server=object(), tool_names=["bash"])
    assert "WebSearch" in opts.allowed_tools
    assert "WebSearch" not in opts.disallowed_tools
    # WebFetch not declared → stays disallowed, not allowlisted.
    assert "WebFetch" not in opts.allowed_tools
    assert "WebFetch" in opts.disallowed_tools


def test_no_web_declared_keeps_builtins_disallowed(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-6")
    opts = runner._build_options(
        _inv(tmp_path, frozenset({"complete_step"})),
        mcp_server=object(), tool_names=["bash"])
    assert "WebSearch" in opts.disallowed_tools
    assert "WebFetch" in opts.disallowed_tools
    assert "WebSearch" not in opts.allowed_tools


def test_toolsearch_still_exempt(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-6")
    opts = runner._build_options(
        _inv(tmp_path, frozenset({"complete_step", "web_search"})),
        mcp_server=object(), tool_names=["bash"])
    assert "ToolSearch" not in opts.disallowed_tools
