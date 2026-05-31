# tests/test_anthropic_runner.py
from __future__ import annotations

from pathlib import Path

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
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
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
         interactive=False, user_input_fn=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path / "art"),
        interactive=interactive, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
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
    assert [e["type"] for e in yielded] == ["agent_output"]
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
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path / "art"),
        interactive=False, transcript_dir=inv_dir,
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log,
        retry_feedback="Gate `test` failed (exit 1). FAIL: assertion",
        balance_on_resume=True,
    )

    yielded = [ev async for ev in runner.run(inv)]

    # SEND_TO_LLM path taken (no missing_complete_step error), agent re-completed.
    assert [e["type"] for e in yielded] == ["agent_output"]
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
    state = classify_resume(msgs, interactive=False)
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
        _agent_def(tools=frozenset({"complete_step", "task"})),
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
        response(tool_use("t1", "task", {
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
async def test_interactive_resume_from_prompt_user_prompts_then_continues(
    tmp_path: Path,
) -> None:
    """Interactive resume: transcript ends with text-only assistant → runner
    must prompt user, append the response, then drive the next LLM call."""
    from dataclasses import replace

    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, interactive=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate transcript: user said "go", assistant replied text-only.
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(TextBlock(text="what about X?"),),
    ))

    # After the resume prompt, the user types "do X"; the runner sends one LLM
    # call which completes the step.
    fake = FakeAsyncAnthropic([
        response(
            tool_use("c1", "complete_step", {"status": "Success"}),
            stop_reason="tool_use",
        ),
    ])

    user_inputs = iter(["do X"])
    log = InMemoryEventLog()

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    runner._user_input_fn = lambda _prompt: next(user_inputs)

    async for _ in runner.run(replace(inv, event_log=log)):
        pass

    types = [e["type"] for e in log.read()]
    # user_message before llm_response in the log
    assert types.index("user_message") < types.index("llm_response")

    # Transcript now has 4 messages: user("go") / assistant("what about X?")
    # / user("do X") / assistant(complete_step). Original two preserved.
    loaded = t.load()
    assert len(loaded) == 4
    assert loaded[2].role == "user"
    assert isinstance(loaded[2].content[0], TextBlock)
    assert loaded[2].content[0].text == "do X"


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
    assert [e["type"] for e in events] == ["agent_output"]
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
async def test_runner_forwards_model_tier_to_subagent(tmp_path: Path) -> None:
    """Full provider path: the model emits a `task` call carrying
    model_tier='powerful'; the runner must forward it so the subagent spawns on
    the powerful tier's resolved (cross-vendor) model. A dropped forward would
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
        source=SourceLoc(file=Path("r.md"), line=1))   # no model_role
    parent_def = dataclasses.replace(
        _agent_def(tools=frozenset({"complete_step", "task"})),
        subagents=("reviewer",))

    log = InMemoryEventLog()
    inv = dataclasses.replace(
        _inv(tmp_path, event_log=log),
        agent_def=parent_def,
        subagent_defs={"reviewer": reviewer_def},
        model_tiers=("powerful",),
        resolved_roles={"powerful": ProviderModel("openai", "gpt-5.2")})

    parent_fake = FakeAsyncAnthropic([
        response(tool_use("t1", "task", {
            "subagent_name": "reviewer", "task_description": "hard one",
            "model_tier": "powerful"})),
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
        assert child["model"] == "gpt-5.2"      # forwarded → tier model
    finally:
        _resolve.reset_providers_for_tests()
