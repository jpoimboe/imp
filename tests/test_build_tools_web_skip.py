from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from imp.providers.anthropic import _build_tools as anthropic_build
from imp.providers.claude_code import _build_tools as cc_build
from imp.providers.openai import _build_tools as openai_build


def _agent(tools: frozenset[str], explicit_tools: bool = False) -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(fields=(), source=loc),
        prompt_template_source="", source_path=Path("a.md"), source=loc,
        explicit_tools=explicit_tools)


def _inv(provider: str, tools: frozenset[str], explicit_tools: bool = False) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(tools, explicit_tools=explicit_tools), provider_name=provider,
        model_name="m", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(handle="s", title="t", project_root=Path("/"),
                                     workflow_dir=Path("/")),
        transcript_dir=Path("/"),
        fs_permissions=(), events_path=Path("/events.jsonl"))


@pytest.mark.parametrize("build,provider", [
    (anthropic_build, "anthropic"),
    (cc_build, "claude_code"),
    (openai_build, "openai"),
])
def test_web_search_is_skipped_not_registry_looked_up(build, provider) -> None:
    tools = build(_inv(provider, frozenset({"complete_step", "web_search"})))
    # web_search is reserved → never added to the tool dict.
    assert "web_search" not in tools
    assert "complete_step" in tools


def test_openai_web_fetch_declaration_degrades_gracefully() -> None:
    # web_fetch on openai is dropped, not raised; the reserved name also never
    # enters the registry tool dict.
    tools = openai_build(_inv("openai", frozenset({"complete_step", "web_fetch"})))
    assert "web_fetch" not in tools
    assert "complete_step" in tools


def test_openai_researcher_toolset_loads() -> None:
    # The researcher declares both web tools; under openai it must load with
    # web_fetch silently dropped (and both reserved names absent from the dict).
    tools = openai_build(_inv(
        "openai",
        frozenset({"complete_step", "read_file", "glob", "grep", "bash",
                   "query_events", "web_search", "web_fetch"})))
    assert "web_fetch" not in tools and "web_search" not in tools
    assert {"complete_step", "read_file", "glob", "grep", "bash",
            "query_events"} <= set(tools)


def test_anthropic_web_fetch_declaration_is_allowed() -> None:
    tools = anthropic_build(_inv("anthropic", frozenset({"complete_step", "web_fetch"})))
    assert "web_fetch" not in tools  # reserved, not a registry tool
