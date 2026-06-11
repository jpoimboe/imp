# tests/test_resume_entry_drain.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.agent_run import drain_all_interjects_at_resume


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_drain_all_appends_in_order_and_clears() -> None:
    class _Coord:
        def __init__(self) -> None:
            self._q = ["one", "two"]
        def drain_interject(self):
            return self._q.pop(0) if self._q else None
    coord = _Coord()
    # The provider `messages` list (raw Messages appended) is SEPARATE from the
    # fake transcript/elog sink list — otherwise bare Messages and tagged tuples
    # land in one list and the comprehension below tries to unpack a frozen
    # Message (TypeError).
    messages: list = []
    sink: list = []
    class _T:
        def append(self, m): sink.append(("t", m))
    class _E:
        def append_user_message(self, *, agent_id, message): sink.append(("u", message))
        def append(self, **k): sink.append(("e", k))
    delivered = drain_all_interjects_at_resume(coord, messages, _T(), _E(), "aid")
    assert [m.content[0].text for m in messages] == ["one", "two"]
    assert [m.content[0].text for tag, m in sink if tag == "t"] == ["one", "two"]
    assert delivered == 2
    assert coord.drain_interject() is None  # queue cleared


@pytest.mark.anyio
async def test_anthropic_resume_entry_drain_before_first_send(tmp_path: Path) -> None:
    """Interjects queued before resume must appear in the FIRST messages.create
    call, not one turn late."""
    from imp.agents.runner import (
        AgentInvocation,
        ChangeContext,
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    # Build a minimal agent def that has complete_step + read_file.
    agent_def = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "read_file"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)

    # Seed a transcript that ends with a user tool_result (RESUME_NORMAL state)
    # so the provider takes the resume branch without AWAITING_USER.
    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))

    # Coordinator with two queued interjects.
    class _FakeCoordinator:
        def __init__(self) -> None:
            self._q = ["interject-A", "interject-B"]

        def drain_interject(self) -> str | None:
            return self._q.pop(0) if self._q else None

        def reset(self) -> None:
            pass

    coord = _FakeCoordinator()

    # Fake client: complete on the first call (so we only need one response).
    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: fake,
    )

    inv = AgentInvocation(
        agent_def=agent_def,
        provider_name="anthropic",
        model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",),
            mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
    )

    events = [e async for e in runner.run(inv)]

    # Run completed successfully.
    assert events[-1]["type"] == "agent_output"

    # The FIRST messages.create call must include BOTH interject texts
    # (appended right after the 3 transcript messages).
    first_call = fake.messages.calls[0]
    first_call_messages = first_call["messages"]

    # Extract text content from all user messages in the first call.
    all_texts = []
    for m in first_call_messages:
        if m.get("role") == "user":
            for block in m.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    all_texts.append(block["text"])

    assert "interject-A" in all_texts, (
        f"interject-A missing from first request messages: {first_call_messages}"
    )
    assert "interject-B" in all_texts, (
        f"interject-B missing from first request messages: {first_call_messages}"
    )
    # Both interjects consumed (queue cleared).
    assert coord.drain_interject() is None

