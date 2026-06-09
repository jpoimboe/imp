from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.cli.signals import InterruptCoordinator
from imp.engine.agent_run import deliver_interject
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    FsRule,
    OutputField,
    OutputSchema,
    SourceLoc,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_enqueue_then_drain_is_fifo():
    c = InterruptCoordinator()
    assert c.drain_interject() is None
    c.enqueue_interject("first")
    c.enqueue_interject("second")
    assert c.drain_interject() == "first"
    assert c.drain_interject() == "second"
    assert c.drain_interject() is None


class _FakeTranscript:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def append(self, msg: Message) -> None:
        self.messages.append(msg)


def test_deliver_interject_appends_user_message_and_emits_event():
    coord = InterruptCoordinator()
    coord.enqueue_interject("look at the README")
    messages: list[Message] = []
    transcript = _FakeTranscript()
    elog = InMemoryEventLog()

    delivered = deliver_interject(coord, messages, transcript, elog, "step:a")

    assert delivered is True
    # Appended to BOTH messages and transcript as a user TextBlock message.
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert isinstance(messages[0].content[0], TextBlock)
    assert messages[0].content[0].text == "look at the README"
    assert transcript.messages == messages
    # Emitted both a user_message and a user_interjected event.
    types = [e["type"] for e in elog.read()]
    assert "user_message" in types
    assert "user_interjected" in types
    interjected = next(e for e in elog.read() if e["type"] == "user_interjected")
    assert interjected["payload"]["text"] == "look at the README"
    # FIFO queue is now drained.
    assert coord.drain_interject() is None


def test_deliver_interject_noop_when_queue_empty():
    coord = InterruptCoordinator()
    messages: list[Message] = []
    transcript = _FakeTranscript()
    elog = InMemoryEventLog()
    assert deliver_interject(coord, messages, transcript, elog, "step:a") is False
    assert messages == []
    assert list(elog.read()) == []


def test_deliver_interject_noop_when_coordinator_none():
    assert deliver_interject(None, [], _FakeTranscript(), InMemoryEventLog(),
                             "step:a") is False


# --- provider turn-boundary delivery tests --------------------------------


def _agent_def(tools=frozenset({"complete_step", "read_file"})) -> AgentDef:
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="note", type="string", required=False),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, *, event_log, coordinator, tools=None,
         agent_id="step:p/s") -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id=agent_id,
        event_log=event_log,
        coordinator=coordinator,
    )


@pytest.mark.anyio
async def test_anthropic_delivers_interject_at_turn_boundary(tmp_path: Path) -> None:
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("payload")
    # Turn 1 makes a tool call (creates a turn boundary); turn 2 completes.
    fake = FakeAsyncAnthropic([
        response(tool_use("u1", "read_file", {"path": "/project/data.txt"})),
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
    ])
    log = InMemoryEventLog()
    coord = InterruptCoordinator()
    # A human types a line while the agent runs (before turn 2's boundary).
    coord.enqueue_interject("please also check the tests")

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake)
    events = [e async for e in runner.run(
        _inv(tmp_path, event_log=log, coordinator=coord))]

    assert "agent_output" in [e["type"] for e in events]
    # user_interjected was emitted (at the turn boundary, not mid-turn).
    log_events = list(log.read())
    interjected = [e for e in log_events if e["type"] == "user_interjected"]
    assert len(interjected) == 1
    assert interjected[0]["payload"]["text"] == "please also check the tests"
    # The interject rode into turn 2's model call as a user TextBlock message.
    second_call = fake.messages.calls[1]
    texts = [
        block.get("text")
        for m in second_call["messages"] if m["role"] == "user"
        for block in m["content"] if block.get("type") == "text"
    ]
    assert "please also check the tests" in texts
    # Queue drained: only one delivery.
    assert coord.drain_interject() is None


@pytest.mark.anyio
async def test_openai_delivers_interject_at_turn_boundary(tmp_path: Path) -> None:
    from imp.agents.runner import ChangeContext as _CC
    from imp.providers.openai import OpenAIAgentRunner
    from tests.openai_fakes import (
        FakeResponsesClient,
        resp_function_call,
        resp_message,
        resp_response,
    )

    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeResponsesClient([
        resp_response(
            resp_message("reading"),
            resp_function_call("u1", "read_file", '{"path": "/project/data.txt"}'),
        ),
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'),
        ),
    ])
    log = InMemoryEventLog()
    coord = InterruptCoordinator()
    coord.enqueue_interject("please also check the tests")

    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=_CC(
            id="test", slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
    )
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini", client_factory=lambda: fake)
    events = [e async for e in runner.run(inv)]

    assert "agent_output" in [e["type"] for e in events]
    interjected = [e for e in log.read() if e["type"] == "user_interjected"]
    assert len(interjected) == 1
    assert interjected[0]["payload"]["text"] == "please also check the tests"
    # The interject rode into turn 2's model call as a user input_text item.
    second_call = fake.responses.calls[1]
    texts = [
        c.get("text")
        for item in second_call["input"]
        if item.get("type") == "message" and item.get("role") == "user"
        for c in item.get("content", [])
    ]
    assert "please also check the tests" in texts
    assert coord.drain_interject() is None


@pytest.mark.anyio
async def test_claude_code_delivers_interject_at_requery_boundary(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from imp.agents.runner import ChangeContext as _CC
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from tests.cc_fakes import (
        FakeClaudeSDKClient,
        assistant,
        result,
        text,
        tool_use,
    )

    (tmp_path / "art").mkdir()
    # Turn 1 ends WITHOUT complete_step → continuation_fn nudges → re-query
    # (the runner's only turn boundary). Turn 2 completes.
    scripted = [
        assistant(text("still thinking")),
        result(),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    coord = InterruptCoordinator()
    coord.enqueue_interject("please also check the tests")

    inv = AgentInvocation(
        agent_def=_agent_def(tools=frozenset({"complete_step"})),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=_CC(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
    )
    inv = replace(inv, continuation_fn=lambda _last: "call complete_step now")

    # Capture the client so we can read what was re-queried.
    captured: dict[str, Any] = {}

    def capturing_factory(opts: Any) -> FakeClaudeSDKClient:
        c = factory(opts)
        captured["client"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=capturing_factory)
    events = [e async for e in runner.run(inv)]

    assert "agent_output" in [e["type"] for e in events]
    interjected = [e for e in log.read() if e["type"] == "user_interjected"]
    assert len(interjected) == 1
    assert interjected[0]["payload"]["text"] == "please also check the tests"
    # The re-query (queries[1]) folded the interject text into the next prompt.
    requery = captured["client"].queries[1]
    assert "please also check the tests" in requery
    assert coord.drain_interject() is None
