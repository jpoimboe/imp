# tests/test_anthropic_runner.py
from __future__ import annotations

from pathlib import Path

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


def _inv(tmp_path: Path, tools=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path / "art",
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(
            text("calling complete_step"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
    ])

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: fake,
    )
    events = []
    async for ev in runner.run(_inv(tmp_path)):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "agent_message" in kinds
    assert kinds[-1] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success", "note": "ok"}


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

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path))]
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
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
async def test_thinking_block_emits_warning_not_error(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        # Response carries thinking + a complete_step tool_use.
        response(
            thinking("considering the options..."),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path))]
    kinds = [e["type"] for e in events]
    # Must emit at least one runner_warning and reach agent_output (no error).
    assert "runner_warning" in kinds
    warn_payloads = [e["payload"]["message"] for e in events if e["type"] == "runner_warning"]
    assert any("thinking" in m for m in warn_payloads)
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
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path))]
    assert any(e["type"] == "runner_warning" for e in events)
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


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_runner_dispatches_task_via_subagent(tmp_path: Path) -> None:
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

    inv = dataclasses.replace(
        _inv(tmp_path),
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

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: parent_fake,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["status"] == "Success"

    # The parent's tool_result for the task call should carry the
    # JSON-stringified subagent agent_output.
    tool_results = [e for e in events if e["type"] == "tool_result"]
    task_result = next(e for e in tool_results if e["payload"]["id"] == "t1")
    import json
    parsed = json.loads(task_result["payload"]["content"])
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
    inv = _inv(tmp_path)
    inv = replace(inv, interactive=True)
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

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    # The runner injects user_input_fn from ctx; the executor would normally
    # supply it. For this unit test, install it directly by patching
    # ToolContext after construction — same pattern as test_ask_user_tool.py.
    # Easiest: monkey-patch the runner's `_make_spawn_subagent` and rely on
    # the executor-style hook. For Plan 3 we expose a constructor kwarg.
    runner._user_input_fn = lambda _prompt: next(user_inputs)

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    kinds = [e["type"] for e in events]
    # The new user_input event fires BEFORE any agent_message.
    user_input_idx = kinds.index("user_input")
    agent_message_idx = kinds.index("agent_message")
    assert user_input_idx < agent_message_idx
    assert events[user_input_idx]["payload"]["text"] == "do X"
    assert kinds[-1] == "agent_output"

    # Transcript now has 4 messages: user("go") / assistant("what about X?")
    # / user("do X") / assistant(complete_step). Original two preserved.
    loaded = t.load()
    assert len(loaded) == 4
    assert loaded[2].role == "user"
    assert isinstance(loaded[2].content[0], TextBlock)
    assert loaded[2].content[0].text == "do X"
