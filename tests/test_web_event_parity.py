from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import _emit_anthropic_web_events
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.openai import _emit_openai_web_events
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_result, tool_use, user
from tests.openai_fakes import resp_message, resp_response, resp_web_search_call


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _shape(elog: InMemoryEventLog) -> dict:
    events = list(elog.read())
    call = next(e for e in events
                if e["type"] == "tool_call" and e["payload"]["name"] == "web_search")
    result_ev = next(e for e in events
                     if e["type"] == "tool_result" and e["payload"].get("citations"))
    return {
        "call_name": call["payload"]["name"],
        "call_keys": set(call["payload"].keys()),
        "result_keys": set(result_ev["payload"].keys()),
        "first_citation_keys": set(result_ev["payload"]["citations"][0].keys()),
    }


def _anthropic_shape() -> dict:
    raw = {"content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search",
         "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1",
         "content": [{"type": "web_search_result", "url": "https://a.test",
                      "title": "A"}]},
    ]}
    el = InMemoryEventLog()
    _emit_anthropic_web_events(raw, el, "a")
    return _shape(el)


def _openai_shape() -> dict:
    raw = resp_response(
        resp_web_search_call("s1", query="q"),
        resp_message("x", citations=[{"url": "https://a.test", "title": "A"}]),
    ).model_dump(mode="json")
    el = InMemoryEventLog()
    _emit_openai_web_events(raw, el, "a")
    return _shape(el)


async def _cc_shape(tmp_path: Path) -> dict:
    loc = SourceLoc(file=Path("a.md"), line=1)
    agent = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "web_search"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc)
    el = InMemoryEventLog()
    inv = AgentInvocation(
        agent_def=agent, provider_name="claude_code", model_name="m",
        system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=el)
    scripted = [
        assistant(tool_use("s1", "WebSearch", {"query": "q"})),
        user(tool_result("s1", 'Links: [{"title":"A","url":"https://a.test"}]')),
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    runner = ClaudeCodeAgentRunner(
        "m", client_factory=lambda opts: FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    async for _ in runner.run(inv):
        pass
    return _shape(el)


@pytest.mark.anyio
async def test_all_three_providers_emit_identical_web_event_shape(tmp_path: Path) -> None:
    a = _anthropic_shape()
    o = _openai_shape()
    c = await _cc_shape(tmp_path)
    for s in (a, o, c):
        assert s["call_name"] == "web_search"
        assert s["call_keys"] == {"id", "name", "input"}
        # tool_result payload always {id, content, error} + optional citations.
        assert "citations" in s["result_keys"]
        # Only `url` is guaranteed across providers (§3).
        assert "url" in s["first_citation_keys"]
    # The three shapes are mutually identical in their structural keys.
    assert a["call_keys"] == o["call_keys"] == c["call_keys"]
    assert a["result_keys"] == o["result_keys"] == c["result_keys"]
