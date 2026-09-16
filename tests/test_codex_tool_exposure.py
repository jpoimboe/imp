"""Static fast-suite guards on the imp tool names codex can see and the names
imp's codex-facing prompts tell it to call.

The live probe prompt runs only under `--run-live`, so a stale
`mcp__imp__`-prefixed instruction in it would not surface until the next live
run — this file catches it with no codex process involved.
"""
from __future__ import annotations

from pathlib import Path

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.agents.tools import get_registry
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from imp.providers.codex_runner import _build_tools as codex_build_tools
from imp.providers.web_native import WEB_TOOL_NAMES


def test_live_codex_prompts_name_imp_tools_by_their_bare_names() -> None:
    src = (Path(__file__).resolve().parent / "test_codex_provider_live.py").read_text()
    assert "mcp__imp__" not in src


# codex 0.154.0's model-visible native tool names: the `functions` namespace
# (exec, wait, request_user_input) plus the declarations codex exposes inside
# exec (measured, .imp/quests/maso03/spike/tool-surface-ab.txt).
_CODEX_NATIVE_TOOL_NAMES = frozenset({
    "apply_patch", "create_goal", "exec", "get_goal",
    "list_mcp_resource_templates", "list_mcp_resources", "read_mcp_resource",
    "request_user_input", "update_goal", "view_image", "wait",
})


def _codex_visible_imp_tool_names() -> set[str]:
    """Every imp tool name codex can see, derived from the real code paths:
    `_build_tools`' output (the registry subset plus the synthesized
    complete_step / sub_agent / load_skill) plus the reserved web names it skips
    and routes to native injection."""
    loc = SourceLoc(file=Path("a.md"), line=1)
    declared = (frozenset(get_registry())
                | {"complete_step", "sub_agent", "load_skill"}
                | WEB_TOOL_NAMES)
    agent = AgentDef(
        name="t", description="", tools=declared, subagents=(),
        output_schema=OutputSchema(fields=(), source=loc),
        prompt_template_source="", source_path=Path("a.md"), source=loc)
    inv = AgentInvocation(
        agent_def=agent, provider_name="codex", model_name="m", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="t", project_root=Path("/"),
                                   workflow_dir=Path("/")),
        transcript_dir=Path("/"), events_path=Path("/events.jsonl"))
    return set(codex_build_tools(inv)) | set(WEB_TOOL_NAMES)


def test_no_imp_tool_name_collides_with_a_codex_native() -> None:
    names = _codex_visible_imp_tool_names()
    # Non-vacuity: all three exposure paths must really be covered, or the
    # disjointness assertion below could pass on a near-empty set.
    assert {"read_file", "bash", "complete_step", "sub_agent", "load_skill",
            "web_search", "web_fetch"} <= names
    assert names & _CODEX_NATIVE_TOOL_NAMES == set()
