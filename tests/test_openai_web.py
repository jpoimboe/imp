from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import OpenAIAgentRunner, _emit_openai_web_events
from tests.openai_fakes import (
    FakeResponsesClient,
    resp_function_call,
    resp_message,
    resp_response,
    resp_web_search_call,
)


def test_emit_web_search_event_with_citations() -> None:
    raw = resp_response(
        resp_web_search_call("ws1", query="imp agents"),
        resp_message("Answer.", citations=[
            {"url": "https://a.test", "title": "A"}]),
    ).model_dump(mode="json")
    elog = InMemoryEventLog()
    _emit_openai_web_events(raw, elog, "step:p/s")
    events = list(elog.read())
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [c["payload"]["name"] for c in calls] == ["web_search"]
    assert calls[0]["payload"]["input"] == {"query": "imp agents"}
    assert results[0]["payload"]["citations"] == [
        {"url": "https://a.test", "title": "A"}]


def test_no_web_search_call_emits_nothing() -> None:
    raw = resp_response(resp_message("hi")).model_dump(mode="json")
    elog = InMemoryEventLog()
    _emit_openai_web_events(raw, elog, "a")
    assert [e for e in elog.read() if e["type"] in ("tool_call", "tool_result")] == []


# ── Step 5: runner-level injection test — web_search spec reaches responses.create ──

@pytest.fixture
def anyio_backend():
    return "asyncio"


def _web_agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step", "web_search"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _web_inv(tmp_path: Path, *, event_log=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_web_agent_def(),
        provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            handle="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=event_log,
    )


@pytest.mark.anyio
async def test_web_search_spec_injected_into_responses_create(tmp_path: Path) -> None:
    """An agent declaring web_search must have {"type":"web_search"} in the
    tools list passed to responses.create (Task 17 wiring; no test existed)."""
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )
    async for _ in runner.run(_web_inv(tmp_path, event_log=log)):
        pass

    tools_arg = fake.responses.calls[0]["tools"]
    assert {"type": "web_search"} in tools_arg
