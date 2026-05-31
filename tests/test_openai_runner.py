# tests/test_openai_runner.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

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
from imp.providers.openai import OpenAIAgentRunner
from tests.openai_fakes import FakeAsyncOpenAI, completion, message, tool_call


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
        provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path / "art",
        ),
        interactive=interactive, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
        agent_id=agent_id,
        event_log=event_log,
        user_input_fn=user_input_fn,
    )


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncOpenAI([
        completion(message(
            content="calling complete_step",
            tool_calls=[tool_call("c1", "complete_step",
                                  {"status": "Success", "note": "ok"})],
        )),
    ])

    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )
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
    assert yielded[-1]["payload"] == {"status": "Success", "note": "ok"}


@pytest.mark.anyio
async def test_resume_from_existing_transcript(tmp_path: Path) -> None:
    """If transcript.jsonl exists, runner reloads it and continues mid-step."""
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    (inv.transcript_dir).mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="reading first…"),
            ToolUseBlock(id="t0", name="read_file", input={"path": "/project/x"}),
        ),
    ))
    t.append(Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t0", content="some content"),),
    ))

    # The runner sees the SEND_TO_LLM resume state; next call is the agent's
    # next turn — and it completes the step.
    fake = FakeAsyncOpenAI([
        completion(message(
            content="done",
            tool_calls=[tool_call("c1", "complete_step", {"status": "Success"})],
        )),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert kinds[-1] == "agent_output"
    # Initial-messages were NOT re-appended because transcript was non-empty.
    loaded = t.load()
    # Original 3 + new assistant turn = 4 messages.
    assert len(loaded) == 4


@pytest.mark.anyio
async def test_refusal_emits_runner_warning(tmp_path: Path) -> None:
    """OpenAI's `refusal` field is not part of imp Message; should warn, not fail."""
    (tmp_path / "art").mkdir()

    fake = FakeAsyncOpenAI([
        # First response: only a refusal. No tool_calls → runner falls through
        # to "agent finished without complete_step" error (autonomous mode).
        completion(message(content=None, refusal="I can't help with that.")),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )
    events = []
    async for ev in runner.run(_inv(tmp_path, event_log=log)):
        events.append(ev)

    log_types = [e["type"] for e in log.read()]
    assert "runner_warning" in log_types
    warnings = [e for e in log.read() if e["type"] == "runner_warning"]
    assert any("refusal" in w["payload"]["message"] for w in warnings)
    # No fatal — the final event is `error` (no complete_step), not `error` for
    # an unknown block.
    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["kind"] == "no_complete_step"


@pytest.mark.anyio
async def test_tool_call_ceiling_enforced(tmp_path: Path) -> None:
    """The runner refuses to dispatch tool calls beyond the agent's ceiling
    and emits both per-call ceiling-error tool_results and a final error event."""
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    (tmp_path / "f.txt").write_text("x")
    project_rules = (FsRule(
        operations=("read",), paths=("/project/**",), mode="allow",
        source=SourceLoc(file=Path("w.yaml"), line=1),
    ),)

    inv = _inv(tmp_path)
    inv = replace(inv, fs_permissions=project_rules, max_tool_calls=2)

    # Two assistant turns, each issuing two tool_use blocks. After 2 calls
    # the ceiling trips on the 3rd; the rest are backfilled with errors.
    fake = FakeAsyncOpenAI([
        completion(message(
            content=None,
            tool_calls=[
                tool_call("c1", "read_file", {"path": "/project/f.txt"}),
                tool_call("c2", "read_file", {"path": "/project/f.txt"}),
            ],
        )),
        completion(message(
            content=None,
            tool_calls=[
                tool_call("c3", "read_file", {"path": "/project/f.txt"}),
                tool_call("c4", "read_file", {"path": "/project/f.txt"}),
            ],
        )),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    errors = [e for e in events if e["type"] == "error"]
    assert any(e["payload"].get("kind") == "tool_call_ceiling_exceeded" for e in errors)


@pytest.mark.anyio
async def test_runs_subagent_via_task(tmp_path: Path) -> None:
    """task tool dispatches to a same-provider child runner via the resolve
    registry; result flows back as the parent's tool_result content."""
    import json
    from dataclasses import replace

    from imp.providers import resolve as _resolve
    from imp.providers.openai import OpenAIProvider

    (tmp_path / "art").mkdir()

    sub_def = AgentDef(
        name="sub", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(
                name="status", type="enum",
                values=("Success", "Error"), required=True,
            ),),
            source=SourceLoc(file=Path("sub.md"), line=1),
        ),
        prompt_template_source="hi from sub",
        source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1),
    )

    parent_def = _agent_def(tools=frozenset({"complete_step", "task"}))
    inv = _inv(tmp_path)
    inv = replace(
        inv,
        agent_def=parent_def,
        subagent_defs={"sub": sub_def},
    )

    # Sequence:
    #   1. parent emits task(sub, ...)
    #   2. sub emits complete_step(status=Success)  ← consumed from fake queue
    #   3. parent receives JSON tool_result, emits complete_step
    fake = FakeAsyncOpenAI([
        # parent turn 1
        completion(message(
            content="dispatching",
            tool_calls=[tool_call("p1", "task",
                                  {"subagent_name": "sub",
                                   "task_description": "do work"})],
        )),
        # sub turn 1 (uses the same fake — order matters)
        completion(message(
            content="sub finishing",
            tool_calls=[tool_call("s1", "complete_step", {"status": "Success"})],
        )),
        # parent turn 2
        completion(message(
            content="all done",
            tool_calls=[tool_call("p2", "complete_step", {"status": "Success"})],
        )),
    ])

    # Register a fake openai provider so spawn_subagent_via_registry builds the
    # child runner with the same shared fake client (not a real network client).
    # Without this, the registry path resolves "openai" to the real factory,
    # which builds an OpenAI client with a stub key and makes a real network
    # call that returns 401.
    _resolve.register_provider(
        "openai", env_check=lambda: True,
        factory=lambda m: OpenAIProvider(
            model_name=m, client_factory=lambda: fake,
        ),
    )

    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )
    events = []
    async for ev in runner.run(replace(inv, event_log=log)):
        events.append(ev)

    # The terminal event is the parent's agent_output, NOT the subagent's.
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success"}
    # tool_call and tool_result go to the log, not the yield stream
    log_types = [e["type"] for e in log.read()]
    assert "tool_call" in log_types
    assert "tool_result" in log_types
    # Verify the subagent's output actually flowed back as the task tool_result.
    # If dispatch regressed (e.g. spawn_subagent_via_registry never called), the
    # task tool_result would be an error string, not the subagent's JSON output.
    task_result_ev = next(
        e for e in log.read()
        if e["type"] == "tool_result" and e["payload"].get("id") == "p1"
    )
    parsed = json.loads(task_result_ev["payload"]["content"])
    assert parsed.get("status") == "Success"
    # All three fake responses must have been consumed: parent turn 1, sub turn
    # 1, parent turn 2. If the subagent ran on the real client instead of fake,
    # the sub's queued response would remain unconsumed here.
    assert len(fake.chat.completions.calls) == 3


@pytest.mark.anyio
async def test_interactive_resume_from_prompt_user_prompts_then_continues(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript
    from tests.openai_fakes import FakeAsyncOpenAI, completion, message, tool_call

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, interactive=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(TextBlock(text="anything else to clarify?"),),
    ))

    fake = FakeAsyncOpenAI([
        completion(message(
            content="done",
            tool_calls=[tool_call("c1", "complete_step", {"status": "Success"})],
        )),
    ])
    user_inputs = iter(["nothing, proceed"])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
        user_input_fn=lambda _prompt: next(user_inputs),
    )

    events = []
    async for ev in runner.run(replace(inv, event_log=log)):
        events.append(ev)

    # user_message before llm_response in the log
    types = [e["type"] for e in log.read()]
    assert types.index("user_message") < types.index("llm_response")
    assert events[-1]["type"] == "agent_output"
    loaded = t.load()
    assert len(loaded) == 4
    assert loaded[2].role == "user"


@pytest.mark.anyio
async def test_runner_interrupt_kills_bash_emits_tool_killed_then_errors(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv = replace(inv, agent_def=_agent_def(
        tools=frozenset({"complete_step", "bash"}),
    ))

    fake = FakeAsyncOpenAI([
        completion(message(
            content=None,
            tool_calls=[tool_call("c1", "bash", {"command": "sleep 10"})],
        )),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake,
    )

    events: list = []
    async def _drain():
        async for ev in runner.run(replace(inv, event_log=log)):
            events.append(ev)

    async def _interrupt():
        await anyio.sleep(0.3)
        await runner.interrupt()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_drain)
        tg.start_soon(_interrupt)

    kinds = [e["type"] for e in events]
    log_types = [e["type"] for e in log.read()]
    # tool_killed is now in the log, not the yield stream
    assert "tool_killed" in log_types
    assert "tool_killed" not in kinds
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["kind"] == "interrupted"

    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    tool_use_ids = {
        b.id for m in loaded if m.role == "assistant"
        for b in m.content if hasattr(b, "id") and hasattr(b, "name")
    }
    tool_result_ids = {
        b.tool_use_id for m in loaded if m.role == "user"
        for b in m.content if hasattr(b, "tool_use_id")
    }
    assert tool_use_ids.issubset(tool_result_ids)


@pytest.mark.anyio
async def test_continuation_resumes_then_completes(tmp_path: Path) -> None:
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeAsyncOpenAI([
        completion(message(content="prose, no tool call")),
        completion(message(tool_calls=[tool_call("c1", "complete_step",
                                                  {"status": "Success"})])),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log),
                  continuation_fn=lambda _last: "please call complete_step")
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"


@pytest.mark.anyio
async def test_continuation_exhaustion_emits_agent_incomplete(tmp_path: Path) -> None:
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeAsyncOpenAI(
        [], default_response=completion(message(content="still prose")),
    )
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    calls: dict[str, int] = {"n": 0}

    def cont(_last: str) -> str | None:
        if calls["n"] >= 2:
            return None
        calls["n"] += 1
        return "please call complete_step"

    inv = replace(_inv(tmp_path, event_log=log), continuation_fn=cont)
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "agent_incomplete"
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_create_passes_stable_prompt_cache_key(tmp_path: Path) -> None:
    """Every create() call within one invocation passes the same
    prompt_cache_key, equal to f"{change_id}:{agent_id}"."""
    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeAsyncOpenAI([
        completion(message(
            tool_calls=[tool_call("u1", "read_file",
                                  {"path": "/project/data.txt"})])),
        completion(message(
            tool_calls=[tool_call("c1", "complete_step",
                                  {"status": "Success", "note": "ok"})])),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path)):
        pass

    calls = fake.chat.completions.calls
    assert len(calls) == 2, f"expected 2 create() calls, got {len(calls)}"
    keys = [c["prompt_cache_key"] for c in calls]
    assert keys == ["1:step:p/s", "1:step:p/s"]


@pytest.mark.anyio
async def test_prompt_cache_key_distinct_per_change(tmp_path: Path) -> None:
    """A different change_id yields a different prompt_cache_key, so two
    concurrent changes running the same step don't share a routing slot."""
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeAsyncOpenAI([
        completion(message(
            tool_calls=[tool_call("c1", "complete_step",
                                  {"status": "Success", "note": "ok"})])),
    ])
    inv = _inv(tmp_path)
    inv = replace(
        inv,
        change_context=ChangeContext(
            change_id=2, project_root=tmp_path, artifacts_dir=tmp_path / "art"),
    )
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(inv):
        pass

    assert fake.chat.completions.calls[0]["prompt_cache_key"] == "2:step:p/s"
