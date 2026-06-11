from __future__ import annotations

from pathlib import Path

from imp.agents.subagents import make_task_tool

# §4 semantics the description MUST convey (derived from the spec, not from the
# implementation string): independent task calls in one message run concurrently,
# and the relative ordering of task vs non-task calls in one message is unspecified.
_CONCURRENT = "run concurrently"
_UNSPECIFIED = "unspecified"


def test_anthropic_task_description_has_elicitation_language() -> None:
    from imp.providers.anthropic import _anthropic_tool_schema
    desc = _anthropic_tool_schema(make_task_tool())["description"]
    assert _CONCURRENT in desc
    assert _UNSPECIFIED in desc


def test_openai_task_description_has_elicitation_language() -> None:
    from imp.providers.openai import _responses_tool_schema
    desc = _responses_tool_schema(make_task_tool())["description"]
    assert _CONCURRENT in desc
    assert _UNSPECIFIED in desc


def test_codex_task_description_has_elicitation_language(tmp_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    from imp.agents.tools.context import ToolContext
    from imp.providers.codex_mcp import RunState, _register_tool
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl", subagent_defs={},
        provider_name="codex", model_name="gpt-5.5",
        parent_transcript_dir=tmp_path,
    )
    app = FastMCP("imp")
    _register_tool(app, make_task_tool(), ctx=ctx, state=RunState(cap=10))
    desc = app._tool_manager._tools["task"].description
    assert _CONCURRENT in desc
    assert _UNSPECIFIED in desc


def test_claude_code_task_description_has_elicitation_language(
    tmp_path: Path,
) -> None:
    from claude_agent_sdk import tool as sdk_tool

    from imp.agents.tools.context import ToolContext
    from imp.providers.claude_code import _RunState, _wrap_one
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    wrapped = _wrap_one("task", make_task_tool(), ctx, _RunState(), sdk_tool)
    assert _CONCURRENT in wrapped.description
    assert _UNSPECIFIED in wrapped.description
