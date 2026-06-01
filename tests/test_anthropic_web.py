from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner


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
        change_context=ChangeContext(slug="s", title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        interactive=False, transcript_dir=tmp_path / "step",
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
