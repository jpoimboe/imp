from __future__ import annotations

# Live follow-up-turn acceptance check (§10 "Anthropic resend acceptance"):
# performed and ACCEPTED during Task 6's live-verification window.  The
# structural tests below (test_replayed_*) pin the necessary condition —
# no citation metadata survives the TextBlock boundary — and supersede any
# remaining owed action from that check.
import json
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.runner import ToolUseBlock as _TUB
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import (
    AnthropicAgentRunner,
    _emit_anthropic_web_events,
    _extract_anthropic_diagnostics,
    _message_to_anthropic,
    _parse_anthropic_message,
)

_WEB_RAW = {
    "content": [
        {"type": "server_tool_use", "id": "srv1", "name": "web_search",
         "input": {"query": "imp agents"}},
        {"type": "web_search_tool_result", "tool_use_id": "srv1",
         "content": [{"type": "web_search_result", "url": "https://a.test",
                      "title": "A"}]},
        {"type": "text", "text": "Answer.",
         "citations": [{"type": "web_search_result_location",
                        "url": "https://a.test", "title": "A",
                        "cited_text": "snippet"}]},
    ],
    "stop_reason": "end_turn",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent(tools: frozenset[str]) -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc)


def _inv(tmp_path: Path, tools: frozenset[str], elog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(tools), provider_name="anthropic",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(handle="s", title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=elog)


class _CaptureMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        from tests.anthropic_fakes import FakeResponse, tool_use
        return FakeResponse(
            content=[tool_use("t1", "complete_step", {"status": "Success"})],
            stop_reason="tool_use")


class _CaptureClient:
    def __init__(self) -> None:
        self.messages = _CaptureMessages()


@pytest.mark.anyio
async def test_web_specs_injected_when_declared(tmp_path: Path) -> None:
    client = _CaptureClient()
    runner = AnthropicAgentRunner("claude-sonnet-4-6",
                                  client_factory=lambda: client)
    elog = InMemoryEventLog()
    inv = _inv(tmp_path, frozenset({"complete_step", "web_search"}), elog)
    async for _ in runner.run(inv):
        pass
    tool_types = {t.get("type") for t in client.messages.calls[0]["tools"]}
    assert "web_search_20250305" in tool_types


@pytest.mark.anyio
async def test_web_specs_absent_when_not_declared(tmp_path: Path) -> None:
    client = _CaptureClient()
    runner = AnthropicAgentRunner("claude-sonnet-4-6",
                                  client_factory=lambda: client)
    inv = _inv(tmp_path, frozenset({"complete_step"}), InMemoryEventLog())
    async for _ in runner.run(inv):
        pass
    tool_types = {t.get("type") for t in client.messages.calls[0]["tools"]}
    assert "web_search_20250305" not in tool_types


def test_web_blocks_produce_no_diagnostic_warnings() -> None:
    diag = _extract_anthropic_diagnostics(_WEB_RAW)
    assert diag.block_warnings == ()


def test_server_tool_use_not_parsed_as_tool_use() -> None:
    msg = _parse_anthropic_message(_WEB_RAW)
    # No dispatchable tool_use; the citation-bearing text survives.
    assert not any(isinstance(b, _TUB) for b in msg.content)
    assert any(getattr(b, "text", "") == "Answer." for b in msg.content)


def test_emit_web_events_search_with_citations() -> None:
    elog = InMemoryEventLog()
    _emit_anthropic_web_events(_WEB_RAW, elog, "step:p/s")
    events = list(elog.read())
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [c["payload"]["name"] for c in calls] == ["web_search"]
    assert calls[0]["payload"]["input"] == {"query": "imp agents"}
    cites = results[0]["payload"]["citations"]
    assert cites == [{"url": "https://a.test", "title": "A",
                      "cited_text": "snippet"}]


def test_emit_web_events_fetch_url_only() -> None:
    raw = {"content": [
        {"type": "server_tool_use", "id": "f1", "name": "web_fetch",
         "input": {"url": "https://doc.test"}},
        {"type": "web_fetch_tool_result", "tool_use_id": "f1",
         "content": {"type": "web_fetch_result", "url": "https://doc.test",
                     "retrieved_at": "2026-01-01T00:00:00Z",
                     "content": {"type": "document", "title": None,
                                 "source": {"type": "text", "data": "body text",
                                            "media_type": "text/plain"}}}},
    ]}
    elog = InMemoryEventLog()
    _emit_anthropic_web_events(raw, elog, "a")
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert results[0]["payload"]["citations"] == [{"url": "https://doc.test"}]
    # Body text from the document source is captured in the audit content.
    assert "body text" in results[0]["payload"]["content"]


def test_emit_web_events_fetch_with_document_title() -> None:
    raw = {"content": [
        {"type": "server_tool_use", "id": "f2", "name": "web_fetch",
         "input": {"url": "https://doc.test"}},
        {"type": "web_fetch_tool_result", "tool_use_id": "f2",
         "content": {"type": "web_fetch_result", "url": "https://doc.test",
                     "content": {"type": "document", "title": "Doc Title",
                                 "source": {"type": "text", "data": "b",
                                            "media_type": "text/plain"}}}},
    ]}
    elog = InMemoryEventLog()
    _emit_anthropic_web_events(raw, elog, "a")
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert results[0]["payload"]["citations"] == [
        {"url": "https://doc.test", "title": "Doc Title"}]


def test_replayed_assistant_drops_origin_blocks_keeps_citation_text() -> None:
    msg = _parse_anthropic_message(_WEB_RAW)
    wire = _message_to_anthropic(msg)
    types = {b["type"] for b in wire["content"]}
    # Origin blocks gone; citation text retained.
    assert "server_tool_use" not in types
    assert "web_search_tool_result" not in types
    assert "text" in types
    assert any(b.get("text") == "Answer." for b in wire["content"])


def test_replayed_text_carries_no_citation_metadata() -> None:
    # The actual resend-safety invariant: no citation metadata survives the
    # TextBlock boundary, so the replayed turn has no dangling
    # web_search_result_location references for Anthropic to reject.
    msg = _parse_anthropic_message(_WEB_RAW)
    wire = _message_to_anthropic(msg)
    for b in wire["content"]:
        assert "citations" not in b
    # Belt-and-suspenders: the serialized turn contains no citation-type marker.
    assert "web_search_result_location" not in json.dumps(wire)


def test_emit_web_events_search_error_is_flagged() -> None:
    raw = {"content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search",
         "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1",
         "content": {"type": "web_search_tool_result_error",
                     "error_code": "max_uses_exceeded"}},
    ]}
    elog = InMemoryEventLog()
    _emit_anthropic_web_events(raw, elog, "a")
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert results[0]["payload"]["error"] is True
    assert "max_uses_exceeded" in results[0]["payload"]["content"]
    assert "citations" not in results[0]["payload"]  # empty → omitted


def test_emit_web_events_fetch_error_is_flagged() -> None:
    raw = {"content": [
        {"type": "server_tool_use", "id": "f1", "name": "web_fetch",
         "input": {"url": "https://unavailable.test"}},
        {"type": "web_fetch_tool_result", "tool_use_id": "f1",
         "content": {"type": "web_fetch_tool_result_error",
                     "error_code": "unavailable"}},
    ]}
    elog = InMemoryEventLog()
    _emit_anthropic_web_events(raw, elog, "a")
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert results[0]["payload"]["error"] is True
    assert "unavailable" in results[0]["payload"]["content"]
    assert "citations" not in results[0]["payload"]  # empty → omitted
