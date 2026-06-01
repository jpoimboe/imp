from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner, _parse_cc_web_citations
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    result,
    tool_result,
    tool_use,
    user,
)


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


# ── Citation parsing unit tests ───────────────────────────────────────────────


def test_parse_websearch_links_citations() -> None:
    content = (
        'Results for "imp agents":\n\n'
        'Some prose.\n\n'
        'Links: [{"title":"A","url":"https://a.test"},'
        '{"title":"B","url":"https://b.test"}]')
    cites = _parse_cc_web_citations("web_search", {"query": "imp agents"}, content)
    assert cites == [
        {"url": "https://a.test", "title": "A"},
        {"url": "https://b.test", "title": "B"}]


def test_parse_websearch_no_links_yields_empty() -> None:
    assert _parse_cc_web_citations("web_search", {"query": "x"}, "no links here") == []


def test_parse_webfetch_uses_input_url() -> None:
    cites = _parse_cc_web_citations(
        "web_fetch", {"url": "https://doc.test"}, "fetched summary text")
    assert cites == [{"url": "https://doc.test"}]


# ── End-to-end stream test ────────────────────────────────────────────────────


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _web_agent() -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "web_search"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc)


def _web_inv(tmp_path: Path, elog: InMemoryEventLog,
             *, max_tool_calls: int | None = None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_web_agent(), provider_name="claude_code",
        model_name="claude-sonnet-4-6", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(slug="s", title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=elog, max_tool_calls=max_tool_calls)


async def _run_cc(scripted: list, inv: AgentInvocation) -> list:
    runner = ClaudeCodeAgentRunner(
        "claude-sonnet-4-6",
        client_factory=lambda opts: FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    out = []
    async for ev in runner.run(inv):
        out.append(ev)
    return out


@pytest.mark.anyio
async def test_cc_web_search_normalized_and_non_counting(tmp_path: Path) -> None:
    scripted = [
        assistant(tool_use("w1", "WebSearch", {"query": "imp agents"})),
        user(tool_result(
            "w1", 'Results.\n\nLinks: [{"title":"A","url":"https://a.test"}]')),
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    elog = InMemoryEventLog()
    # ceiling=1: the web call must NOT consume budget, leaving the single
    # allowed call for complete_step. If the web call counted, complete_step
    # would be the 2nd call and trip the ceiling.
    inv = _web_inv(tmp_path, elog, max_tool_calls=1)
    events = await _run_cc(scripted, inv)

    assert any(e["type"] == "agent_output" for e in events)
    assert not any(e["type"] == "error" for e in events)

    recs = list(elog.read())
    calls = [e for e in recs if e["type"] == "tool_call"]
    results = [e for e in recs if e["type"] == "tool_result"]
    assert any(c["payload"]["name"] == "web_search" for c in calls)
    assert not any(c["payload"]["name"] == "WebSearch" for c in calls)
    web_res = [r for r in results if r["payload"].get("citations")]
    assert web_res[0]["payload"]["citations"] == [
        {"url": "https://a.test", "title": "A"}]
