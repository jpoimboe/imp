# tests/test_anthropic_runner.py
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.agents.transcript import Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from tests._events import control_events
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, thinking, tool_use


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


def _inv(tmp_path: Path, tools=None, *, event_log=None, agent_id="step:p/s",
         user_input_fn=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id=agent_id,
        event_log=event_log,
        user_input_fn=user_input_fn,
    )


def test_agent_invocation_retry_feedback_defaults_none(tmp_path: Path) -> None:
    inv = _inv(tmp_path)
    assert inv.retry_feedback is None


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([response(
        text("calling complete_step"),
        tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
    )])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    yielded = []
    async for ev in runner.run(_inv(tmp_path, event_log=log)):
        yielded.append(ev)

    log_types = [e["type"] for e in log.read()]
    assert "step_context" in log_types
    assert "user_message" in log_types
    assert "llm_response" in log_types
    # audit events are NOT yielded any more — only control signals
    assert [e["type"] for e in control_events(yielded)] == ["agent_output"]
    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert "raw" in resp["payload"]
    assert resp["agent_id"] == "step:p/s"


@pytest.mark.anyio
async def test_dispatches_read_file_then_completes(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeAsyncAnthropic([
        response(tool_use("u1", "read_file", {"path": "/project/data.txt"})),
        response(
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
    ])

    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]
    kinds = [e["type"] for e in events]
    # control signals only: just agent_output
    assert "agent_output" in kinds
    # audit events go to log
    log_types = [e["type"] for e in log.read()]
    assert "tool_call" in log_types
    assert "tool_result" in log_types
    # Tool result was passed back to the LLM in the next .create() call.
    second_call = fake.messages.calls[1]
    last_msg = second_call["messages"][-1]
    assert last_msg["role"] == "user"
    # The user message carries a tool_result block.
    assert any(
        block.get("type") == "tool_result" for block in last_msg["content"]
    )


@pytest.mark.anyio
async def test_transcript_persists_each_turn(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(
            tool_use("c1", "complete_step", {"status": "Success", "note": "n"}),
        ),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    inv = _inv(tmp_path)
    async for _ in runner.run(inv):
        pass
    assert (inv.transcript_dir / "transcript.jsonl").exists()
    lines = (inv.transcript_dir / "transcript.jsonl").read_text().splitlines()
    # at least: initial user + assistant
    assert len(lines) >= 2


@pytest.mark.anyio
async def test_resume_uses_existing_transcript(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    # Seed a transcript with: user msg → assistant tool_use → user tool_result
    inv = _inv(tmp_path)
    transcript = Transcript(inv.transcript_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))

    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "resumed"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["note"] == "resumed"

    # The single LLM call should have been issued with all three prior messages.
    only_call = fake.messages.calls[0]
    assert len(only_call["messages"]) == 3


@pytest.mark.anyio
async def test_resume_with_retry_feedback_injects_and_continues(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    inv_dir = tmp_path / "step"
    transcript = Transcript(inv_dir / "transcript.jsonl")
    # Targeted-step shape: a prior successful complete_step (no tool_result).
    transcript.append(Message(role="user", content=(TextBlock(text="begin"),)))
    transcript.append(Message(role="assistant", content=(
        TextBlock(text="all done"),
        ToolUseBlock(id="c1", name="complete_step", input={"status": "Success"}),
    )))

    # The re-run will fix things and complete again.
    fake = FakeAsyncAnthropic([response(
        tool_use("c2", "complete_step", {"status": "Success", "note": "fixed"}),
    )])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="anthropic",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log,
        retry_feedback="Gate `test` failed (exit 1). FAIL: assertion",
        balance_on_resume=True,
    )

    yielded = [ev async for ev in runner.run(inv)]

    # SEND_TO_LLM path taken (no missing_complete_step error), agent re-completed.
    assert [e["type"] for e in control_events(yielded)] == ["agent_output"]
    assert yielded[-1]["payload"]["note"] == "fixed"
    # The feedback was injected as a user turn balancing the prior complete_step.
    msgs = transcript.load()
    injected = msgs[2]
    assert injected.role == "user"
    assert any(isinstance(b, ToolResultBlock) and not b.is_error
               for b in injected.content)
    assert any(isinstance(b, TextBlock) and "FAIL: assertion" in b.text
               for b in injected.content)


@pytest.mark.anyio
async def test_thinking_block_emits_thinking_event_not_error(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        # Response carries thinking + a complete_step tool_use.
        response(
            thinking("considering the options..."),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]
    # Thinking is captured in the log (not yielded as an event).
    log_types = [e["type"] for e in log.read()]
    assert "thinking" in log_types
    thinking_payloads = [e["payload"]["content"] for e in log.read() if e["type"] == "thinking"]
    assert thinking_payloads == ["considering the options..."]
    # The step still reaches agent_output.
    assert events[-1]["type"] == "agent_output"


@pytest.mark.anyio
async def test_unknown_block_type_emits_warning_not_error(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    # Simulate a future block type by injecting an arbitrary-typed object.
    from dataclasses import dataclass

    @dataclass
    class _FutureBlock:
        type: str = "futurething"
        payload: str = "x"

    from tests.anthropic_fakes import FakeResponse
    weird = FakeResponse(content=[
        _FutureBlock(),
        tool_use("c1", "complete_step", {"status": "Success"}),
    ], stop_reason="tool_use")
    fake = FakeAsyncAnthropic([weird])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]
    log_types = [e["type"] for e in log.read()]
    assert "runner_warning" in log_types
    assert events[-1]["type"] == "agent_output"


@pytest.mark.anyio
async def test_tool_call_ceiling_emits_error(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    # Ceiling = 2; LLM tries 3 read_file calls
    fake = FakeAsyncAnthropic([
        response(tool_use("u1", "read_file", {"path": "/project/x"})),
        response(tool_use("u2", "read_file", {"path": "/project/y"})),
        response(tool_use("u3", "read_file", {"path": "/project/z"})),
    ])
    (tmp_path / "x").write_text("")
    (tmp_path / "y").write_text("")
    import dataclasses
    inv = _inv(tmp_path)
    inv_lo = dataclasses.replace(inv, max_tool_calls=2)

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(inv_lo)]
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "error"
    assert events[-1]["payload"]["kind"] == "tool_call_ceiling_exceeded"


@pytest.mark.anyio
async def test_ceiling_trip_mid_batch_preserves_transcript_invariant(tmp_path: Path) -> None:
    """Spec §6.8 rule 2: every tool_use has a matching tool_result before the
    next assistant turn. If the ceiling trips on the Kth of N tool_uses in
    one assistant message, the remaining N-K must be backfilled with error
    tool_results so resume's classify_resume doesn't see DANGLING_TOOL_USE.
    """
    (tmp_path / "art").mkdir()
    (tmp_path / "x").write_text("")
    (tmp_path / "y").write_text("")
    # One assistant turn with TWO tool_uses; ceiling = 1.
    from tests.anthropic_fakes import FakeResponse, FakeToolUseBlock
    one_turn = FakeResponse(content=[
        FakeToolUseBlock(type="tool_use", id="u1", name="read_file",
                         input={"path": "/project/x"}),
        FakeToolUseBlock(type="tool_use", id="u2", name="read_file",
                         input={"path": "/project/y"}),
    ], stop_reason="tool_use")
    fake = FakeAsyncAnthropic([one_turn])

    import dataclasses
    inv = _inv(tmp_path)
    inv_lo = dataclasses.replace(inv, max_tool_calls=1)

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(inv_lo)]
    assert events[-1]["payload"]["kind"] == "tool_call_ceiling_exceeded"

    # Transcript invariant: classify_resume should NOT report DANGLING_TOOL_USE.
    transcript_path = inv_lo.transcript_dir / "transcript.jsonl"
    msgs = Transcript(transcript_path).load()
    state = classify_resume(msgs)
    assert state is not ResumeState.DANGLING_TOOL_USE


@pytest.mark.anyio
async def test_create_passes_cache_control(tmp_path: Path) -> None:
    """Every messages.create() call must carry cache_control to enable
    Anthropic automatic prompt caching."""
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([response(
        text("calling complete_step"),
        tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
    )])
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path)):
        pass

    assert fake.messages.calls, "no create() call was made"
    for call in fake.messages.calls:
        assert call["cache_control"] == {"type": "ephemeral"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_runner_dispatches_task_via_subagent(tmp_path: Path, provider_registry) -> None:
    """End-to-end runner path: agent declares `task` + subagent; LLM emits a
    tool_use for `task`; runner's special-case `await dispatch_task(...)`
    fires; child runner's agent_output JSON-stringifies into the tool_result.
    """
    import dataclasses

    (tmp_path / "art").mkdir()
    # Parent agent with `task` declared + reviewer subagent ref
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    reviewer_def = AgentDef(
        name="reviewer", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),
                    OutputField(name="verdict", type="enum",
                                values=("approve", "rework"), required=True)),
            source=SourceLoc(file=Path("r.md"), line=1),
        ),
        prompt_template_source="review",
        source_path=Path("r.md"),
        source=SourceLoc(file=Path("r.md"), line=1),
    )
    parent_def = dataclasses.replace(
        _agent_def(tools=frozenset({"complete_step", "sub_agent"})),
        subagents=("reviewer",),
    )

    log = InMemoryEventLog()
    inv = dataclasses.replace(
        _inv(tmp_path, event_log=log),
        agent_def=parent_def,
        subagent_defs={"reviewer": reviewer_def},
    )

    # Parent and child share the queue. Consumption order:
    #   1. parent → tool_use task
    #   2. child  → complete_step with verdict=approve (satisfies reviewer schema)
    #   3. parent → complete_step (status=Success) after task returns
    parent_fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent", {
            "subagent_name": "reviewer", "task_description": "review the spec",
        })),
        response(tool_use("rc", "complete_step",
                          {"status": "Success", "verdict": "approve"})),
        response(tool_use("pc", "complete_step",
                          {"status": "Success", "note": "done"})),
    ])

    # Register a fake anthropic provider so spawn_subagent_via_registry builds
    # the child runner with the same client fake (not a real network client).
    # The shared provider_registry fixture owns reset/teardown.
    from imp.providers.anthropic import AnthropicProvider
    provider_registry(
        "anthropic", env_check=lambda: True,
        factory=lambda m: AnthropicProvider(
            model_name=m, client_factory=lambda: parent_fake,
        ),
    )
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: parent_fake,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["status"] == "Success"

    # tool_call + tool_result are now in the log, not the yield stream.
    log_types = [e["type"] for e in log.read()]
    assert "tool_call" in log_types
    assert "tool_result" in log_types
    # Find the task tool_result in the log and verify it has the subagent output.
    task_result_ev = next(
        e for e in log.read()
        if e["type"] == "tool_result" and e["payload"].get("id") == "t1"
    )
    import json
    parsed = json.loads(task_result_ev["payload"]["content"])
    assert parsed.get("verdict") == "approve"


@pytest.mark.anyio
async def test_bare_turn_end_resume_delivers_reply_then_continues(
    tmp_path: Path,
) -> None:
    """AWAITING_USER resume: a paused transcript ends on a BARE assistant turn
    (text, no complete_step). On resume the runner solicits the human and
    delivers the reply as a plain user TextBlock message (NOT a tool_result),
    then drives the next LLM call to completion."""
    from dataclasses import replace

    from imp.agents.runner import Message, TextBlock, ToolResultBlock
    from imp.agents.transcript import Transcript
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    # user said "go", assistant asked a question and ended its turn.
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="what color?"),)))

    fake = FakeAsyncAnthropic([
        response(
            tool_use("c1", "complete_step", {"status": "Success"}),
            stop_reason="tool_use",
        ),
    ])
    user_inputs = iter(["blue"])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    runner._user_input_fn = lambda _prompt: next(user_inputs)

    async for _ in runner.run(
        replace(inv, event_log=log, balance_on_resume=True)
    ):
        pass

    types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" in types
    assert types.index("user_message") < types.index("llm_response")

    # Transcript: user("go") / assistant(question) / user(TextBlock "blue") /
    # assistant(complete_step). The reply is a plain text turn, not a tool_result.
    loaded = t.load()
    assert len(loaded) == 4
    assert loaded[2].role == "user"
    block = loaded[2].content[0]
    assert isinstance(block, TextBlock)
    assert not isinstance(block, ToolResultBlock)
    assert block.text == "blue"


@pytest.mark.anyio
async def test_runner_interrupt_kills_bash_emits_tool_killed_then_errors(
    tmp_path: Path,
) -> None:
    """End-to-end: while the runner is awaiting bash, interrupt() is called.
    bash returns with signal-killed returncode; runner emits tool_killed +
    interrupted error; transcript is valid (tool_use has its tool_result)."""
    from dataclasses import replace

    from imp.agents.transcript import Transcript
    from tests.anthropic_fakes import (
        FakeAsyncAnthropic,
        response,
    )
    from tests.anthropic_fakes import (
        tool_use as a_tool_use,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv = replace(inv, agent_def=_agent_def(
        tools=frozenset({"complete_step", "bash"}),
    ))

    fake = FakeAsyncAnthropic([
        response(
            a_tool_use("c1", "bash", {"command": "sleep 10"}),
            stop_reason="tool_use",
        ),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )

    events: list = []
    async def _drain_events():
        async for ev in runner.run(replace(inv, event_log=log)):
            events.append(ev)

    async def _interrupt_after_delay():
        await anyio.sleep(0.3)
        await runner.interrupt()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_drain_events)
        tg.start_soon(_interrupt_after_delay)

    kinds = [e["type"] for e in events]
    log_types = [e["type"] for e in log.read()]
    # tool_killed is now in the log, not the yield stream
    assert "tool_killed" in log_types
    assert "tool_killed" not in kinds
    # Last event is the runner's interrupted error (still a control signal yield).
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["kind"] == "interrupted"

    # Transcript invariant: every tool_use has its tool_result.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    tool_use_ids = {
        b.id for m in loaded if m.role == "assistant"
        for b in m.content if hasattr(b, "id") and hasattr(b, "name")
    }
    tool_result_ids = {
        b.tool_use_id for m in loaded if m.role == "user"
        for b in m.content if hasattr(b, "tool_use_id")
    }
    assert tool_use_ids.issubset(tool_result_ids), (
        f"dangling tool_use ids: {tool_use_ids - tool_result_ids}"
    )


@pytest.mark.anyio
async def test_interrupt_cancels_in_flight_request(tmp_path: Path) -> None:
    """C-iii: a stalled messages.create() is cancelled by interrupt() — the run
    returns the 'interrupted' error promptly, not after the request completes."""
    started = anyio.Event()

    class _HangingClient:
        class messages:
            @staticmethod
            async def create(**_kw):
                started.set()
                await anyio.sleep(3600)  # never returns on its own

    runner = AnthropicAgentRunner(
        model_name="claude-x", client_factory=lambda: _HangingClient(),
    )
    inv = _inv(tmp_path)

    events: list = []
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            async def _drive():
                async for ev in runner.run(inv):
                    events.append(ev)

            tg.start_soon(_drive)
            await started.wait()
            await anyio.sleep(0.05)
            await runner.interrupt()

    kinds = [
        e.get("payload", {}).get("kind")
        for e in events
        if e["type"] == "error"
    ]
    assert "interrupted" in kinds


@pytest.mark.anyio
async def test_continuation_resumes_then_completes(tmp_path: Path) -> None:
    """Prose once → continuation injects a nudge → agent then calls
    complete_step → agent_output, no error. The nudge user-message is recorded."""
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(text("here is my answer in prose"), stop_reason="end_turn"),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log),
                  continuation_fn=lambda _last: "please call complete_step")
    events = [e async for e in runner.run(inv)]
    assert [e["type"] for e in control_events(events)] == ["agent_output"]
    user_msgs = [e for e in log.read() if e["type"] == "user_message"]
    # initial "go" + injected nudge
    assert len(user_msgs) == 2


@pytest.mark.anyio
async def test_continuation_exhaustion_emits_agent_incomplete(tmp_path: Path) -> None:
    """Prose every turn; continuation returns None after 2 nudges → the runner
    emits exactly one agent_incomplete error after exactly 2 injected nudges."""
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic(
        [], default_response=response(text("still prose, no tool call"), stop_reason="end_turn"),
    )
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)

    calls = {"n": 0}
    def cont(_last: str) -> str | None:
        if calls["n"] >= 2:
            return None
        calls["n"] += 1
        return "please call complete_step"

    inv = replace(_inv(tmp_path, event_log=log), continuation_fn=cont)
    events = [e async for e in runner.run(inv)]
    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["category"] == "agent_incomplete"
    assert final["payload"]["kind"] == "no_complete_step_after_nudges"
    assert calls["n"] == 2
    user_msgs = [e for e in log.read() if e["type"] == "user_message"]
    assert len(user_msgs) == 3  # initial "go" + 2 nudges


@pytest.mark.anyio
async def test_headless_top_level_nudges_then_pauses(tmp_path: Path) -> None:
    """A headless top-level run (non_interactive=True) nudges NUDGE_LIMIT times,
    then the channel (no client attached) pauses via AwaitingUserInput — a pause,
    not an agent_incomplete error."""
    from dataclasses import replace

    from imp.agents.tools.wait_for_user import AwaitingUserInput
    from imp.engine.completion import NUDGE_LIMIT, build_continuation_fn

    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic(
        [], default_response=response(
            text("still prose, no tool call"), stop_reason="end_turn"),
    )
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)

    def channel(_q: str) -> str:
        # No client attached: the channel pauses the headless run.
        raise AwaitingUserInput()

    cont = build_continuation_fn(
        instruction="CALL complete_step with: done (bool).",
        user_input_fn=channel, event_log=log, agent_id="root",
        non_interactive=True, subagent=False)
    inv = replace(_inv(tmp_path, event_log=log), continuation_fn=cont)

    with pytest.raises(AwaitingUserInput):
        [e async for e in runner.run(inv)]

    assert not any(
        e["type"] == "error"
        and e["payload"].get("category") == "agent_incomplete"
        for e in log.read()
    )
    # The nudges happened before the pause: NUDGE_LIMIT nudge user messages were
    # appended on top of the initial "go" message.
    user_msgs = [e for e in log.read() if e["type"] == "user_message"]
    assert len(user_msgs) == 1 + NUDGE_LIMIT


@pytest.mark.anyio
async def test_runner_forwards_model_tier_to_subagent(tmp_path: Path) -> None:
    """Full provider path: the model emits a `task` call carrying
    tier='high'; the runner must forward it so the subagent spawns on
    the high tier's resolved (cross-vendor) model. A dropped forward would
    inherit the parent's anthropic model and fail the assertion."""
    import dataclasses
    import json as _json

    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    from imp.providers import resolve as _resolve
    from imp.providers.config import ProviderModel

    (tmp_path / "art").mkdir()

    class _RecordingRunner:
        def __init__(self, name: str, model: str) -> None:
            self.name, self.model = name, model

        async def run(self, invocation):  # type: ignore[override]
            yield {"type": "agent_output", "payload": {
                "status": "Success", "ran_on": self.name, "model": self.model}}

        async def interrupt(self) -> None:
            pass

    class _RecordingProvider:
        def __init__(self, name: str, model: str) -> None:
            self.name, self.model = name, model

        def create_runner(self, invocation):
            return _RecordingRunner(self.name, self.model)

    reviewer_def = AgentDef(
        name="reviewer", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("r.md"), line=1)),
        prompt_template_source="review", source_path=Path("r.md"),
        source=SourceLoc(file=Path("r.md"), line=1))
    parent_def = dataclasses.replace(
        _agent_def(tools=frozenset({"complete_step", "sub_agent"})),
        subagents=("reviewer",))

    log = InMemoryEventLog()
    inv = dataclasses.replace(
        _inv(tmp_path, event_log=log),
        agent_def=parent_def,
        subagent_defs={"reviewer": reviewer_def},
        tiers=("high",),
        resolved_tiers={"high": ProviderModel("openai", "gpt-5.5")})

    parent_fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent", {
            "subagent_name": "reviewer", "task_description": "hard one",
            "tier": "high"})),
        response(tool_use("pc", "complete_step", {"status": "Success"})),
    ])

    _resolve.reset_providers_for_tests()
    _resolve.register_provider(
        "openai", env_check=lambda: True,
        factory=lambda m: _RecordingProvider("openai", m))
    _resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda m: _RecordingProvider("anthropic", m))
    try:
        runner = AnthropicAgentRunner(
            model_name="claude-sonnet-4-6", client_factory=lambda: parent_fake)
        events = [e async for e in runner.run(inv)]
        assert events[-1]["type"] == "agent_output"

        task_result = next(
            e for e in log.read()
            if e["type"] == "tool_result" and e["payload"].get("id") == "t1")
        child = _json.loads(task_result["payload"]["content"])
        assert child["ran_on"] == "openai"      # forwarded → tier vendor
        assert child["model"] == "gpt-5.5"      # forwarded → tier model
    finally:
        _resolve.reset_providers_for_tests()


@pytest.mark.anyio
async def test_runner_outer_except_reclassifies_when_cancel_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: object(),
    )

    async def _boom(_inv: object) -> AsyncGenerator[Any, None]:
        if False:  # pragma: no cover — makes this an async generator
            yield {}
        raise RuntimeError("Cannot write to terminated process (exit code: 0)")

    monkeypatch.setattr(runner, "_run_loop", lambda _inv: _boom(_inv))
    runner._cancel = True
    inv = _inv(tmp_path)

    events = [ev async for ev in runner.run(inv)]
    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["category"] == "config"
    assert errors[0]["payload"]["kind"] == "interrupted"
    assert "unclassified provider error" not in errors[0]["payload"]["message"]


@pytest.mark.anyio
async def test_runner_outer_except_unknown_when_cancel_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: object(),
    )

    async def _boom(_inv: object) -> AsyncGenerator[Any, None]:
        if False:  # pragma: no cover — makes this an async generator
            yield {}
        raise RuntimeError("SDK broke unexpectedly")

    monkeypatch.setattr(runner, "_run_loop", lambda _inv: _boom(_inv))
    inv = _inv(tmp_path)
    events = [ev async for ev in runner.run(inv)]
    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["category"] == "unknown"


@pytest.mark.anyio
async def test_request_carries_disable_parallel_tool_use_false(tmp_path: Path) -> None:
    """§4/AC41: every anthropic request explicitly permits parallel tool use."""
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([response(
        tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
    )])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=log)):
        pass
    only_call = fake.messages.calls[0]
    assert only_call["tool_choice"] == {
        "type": "auto", "disable_parallel_tool_use": False,
    }



@pytest.mark.anyio
async def test_resume_preserves_tool_result_and_omits_manifest(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = Transcript(inv.transcript_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),)))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="GROUNDED"),)))

    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    [e async for e in runner.run(inv)]

    sent = json.dumps(fake.messages.calls[0]["messages"])
    # Original tool_result content survives the replay untouched…
    assert "GROUNDED" in sent
    # …and the claude_code-only manifest framing never reaches anthropic.
    assert "replay artifact" not in sent


def test_anthropic_has_no_wait_for_user_result_handling() -> None:
    """No wait_for_user result handling; the _build_tools carve-out is removed."""
    import inspect

    import imp.providers.anthropic as anthropic_mod

    src = inspect.getsource(anthropic_mod)
    assert '_tu.name == "wait_for_user"' not in src
    assert 'if name == "wait_for_user"' not in src


@pytest.mark.anyio
async def test_anthropic_resume_creates_no_native_session_store(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    transcript = Transcript(inv.transcript_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),)))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),)))

    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake)
    events = [e async for e in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"
    # Scope guard: the anthropic path never materializes a claude_session/ store dir.
    assert not (inv.transcript_dir / "claude_session").exists()


@pytest.mark.anyio
async def test_anthropic_llm_response_records_served_model(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([response(
        text("done"),
        tool_use("c1", "complete_step", {"status": "Success"}),
        stop_reason="end_turn", model="claude-served-fallback")])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    async for _ev in runner.run(_inv(tmp_path, event_log=log)):
        pass

    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert resp["payload"]["model"] == "claude-served-fallback"   # served, not configured
    assert "duration_ms" in resp["payload"]
    assert resp["payload"]["duration_ms"] >= 0


@pytest.mark.anyio
async def test_anthropic_llm_response_records_request_id(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    r = response(
        text("done"),
        tool_use("c1", "complete_step", {"status": "Success"}),
        stop_reason="end_turn")
    r._request_id = "req_test_abc123"
    fake = FakeAsyncAnthropic([r])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    async for _ev in runner.run(_inv(tmp_path, event_log=log)):
        pass

    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert resp["payload"]["request_id"] == "req_test_abc123"
