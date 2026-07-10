# tests/test_openai_runner.py
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

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
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import OpenAIAgentRunner
from tests._events import control_events
from tests.openai_fakes import (
    FakeResponsesClient,
    resp_function_call,
    resp_message,
    resp_response,
)


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
         user_input_fn=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            id="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id=agent_id,
        event_log=event_log,
        user_input_fn=user_input_fn,
    )


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(
            resp_message("calling complete_step"),
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'),
        ),
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
    assert [e["type"] for e in control_events(yielded)] == ["agent_output"]
    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert "raw" in resp["payload"]
    assert resp["agent_id"] == "step:p/s"
    assert yielded[-1]["payload"] == {"status": "Success", "note": "ok"}
    # The call carries the Responses surface: instructions/input/tools/store,
    # no Chat Completions `messages`/`system`.
    call = fake.responses.calls[0]
    assert call["instructions"] == "be terse"
    assert "input" in call
    assert "tools" in call
    assert call["store"] is False
    assert "messages" not in call
    assert "system" not in call


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
    fake = FakeResponsesClient([
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
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
    """A function_call with malformed arguments warns, not fails; the turn has
    no dispatchable tool_use (bad JSON → empty args still dispatches), so we use
    a refusal item to exercise the warn-then-no-complete-step path."""
    (tmp_path / "art").mkdir()

    fake = FakeResponsesClient([
        # A refusal item: no message/function_call content → no tool_uses, so
        # the runner falls through to "agent finished without complete_step".
        resp_response({"type": "refusal", "refusal": "I can't help with that."}),
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
    # No fatal — the final event is `error` (no complete_step).
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
    inv = _inv(tmp_path)
    inv = replace(inv, max_tool_calls=2)

    # Two assistant turns, each issuing two tool_use blocks. After 2 calls
    # the ceiling trips on the 3rd; the rest are backfilled with errors.
    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("c1", "read_file", '{"path": "/project/f.txt"}'),
            resp_function_call("c2", "read_file", '{"path": "/project/f.txt"}'),
        ),
        resp_response(
            resp_function_call("c3", "read_file", '{"path": "/project/f.txt"}'),
            resp_function_call("c4", "read_file", '{"path": "/project/f.txt"}'),
        ),
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
    # The backfilled ceiling-error tool_result lands in the transcript.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    err_results = [
        b for m in loaded if m.role == "user"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.is_error
        and "ceiling" in b.content
    ]
    assert err_results, "expected a backfilled ceiling-error tool_result"


@pytest.mark.anyio
async def test_runs_subagent_via_task(tmp_path: Path, provider_registry) -> None:
    """task tool dispatches to a same-provider child runner via the resolve
    registry; result flows back as the parent's tool_result content."""
    from dataclasses import replace

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

    parent_def = _agent_def(tools=frozenset({"complete_step", "sub_agent"}))
    inv = _inv(tmp_path)
    inv = replace(
        inv,
        agent_def=parent_def,
        subagent_defs={"sub": sub_def},
    )

    # Sequence:
    #   1. parent emits task(sub, ...)
    #   2. sub bare-turn-end (no complete_step for subagents) ← consumed from fake queue
    #   3. parent receives text tool_result, emits complete_step
    fake = FakeResponsesClient([
        # parent turn 1
        resp_response(
            resp_message("dispatching"),
            resp_function_call("p1", "sub_agent",
                               '{"subagent_name": "sub",'
                               ' "task_description": "do work"}'),
        ),
        # sub turn 1 — bare turn-end (subagents use final text as deliverable)
        resp_response(
            resp_message("sub finishing"),
        ),
        # parent turn 2
        resp_response(
            resp_message("all done"),
            resp_function_call("p2", "complete_step", '{"status": "Success"}'),
        ),
    ])

    # Register a fake openai provider so spawn_subagent_via_registry builds the
    # child runner with the same shared fake client (not a real network client).
    # Without this, the registry path resolves "openai" to the real factory,
    # which builds an OpenAI client with a stub key and makes a real network
    # call that returns 401. The shared provider_registry fixture owns teardown.
    provider_registry(
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
    assert task_result_ev["payload"]["content"] == "sub finishing"
    # All three fake responses must have been consumed: parent turn 1, sub turn
    # 1, parent turn 2. If the subagent ran on the real client instead of fake,
    # the sub's queued response would remain unconsumed here.
    assert len(fake.responses.calls) == 3


@pytest.mark.anyio
async def test_bare_turn_end_resume_delivers_reply_then_continues(
    tmp_path: Path,
) -> None:
    """AWAITING_USER resume (openai): a paused transcript ends on a bare assistant
    turn; the runner solicits the human and delivers the reply as a plain user
    TextBlock message (not a tool_result) before driving the next LLM call."""
    from dataclasses import replace

    from imp.agents.runner import Message, TextBlock, ToolResultBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="what color?"),)))

    fake = FakeResponsesClient([
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])
    user_inputs = iter(["blue"])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
        user_input_fn=lambda _prompt: next(user_inputs),
    )

    events = []
    async for ev in runner.run(
        replace(inv, event_log=log, balance_on_resume=True)
    ):
        events.append(ev)

    types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" in types
    assert types.index("user_message") < types.index("llm_response")
    assert events[-1]["type"] == "agent_output"
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
    from dataclasses import replace

    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv = replace(inv, agent_def=_agent_def(
        tools=frozenset({"complete_step", "bash"}),
    ))

    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("c1", "bash", '{"command": "sleep 10"}'),
        ),
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
async def test_interrupt_cancels_in_flight_request(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    started = anyio.Event()

    class _HangingClient:
        class responses:
            @staticmethod
            def stream(**_kw):
                class _CM:
                    async def __aenter__(self):
                        started.set()
                        await anyio.sleep(3600)

                    async def __aexit__(self, *_exc):
                        return False
                return _CM()

    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: _HangingClient(),
    )
    inv = _inv(tmp_path)

    events: list = []
    async def _drive():
        async for ev in runner.run(inv):
            events.append(ev)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_drive)
            await started.wait()
            await anyio.sleep(0.05)
            await runner.interrupt()

    kinds = [e.get("payload", {}).get("kind")
             for e in events if e["type"] == "error"]
    assert "interrupted" in kinds


@pytest.mark.anyio
async def test_response_wait_is_request_bracketed(tmp_path: Path) -> None:
    import dataclasses

    from imp.cli.signals import InterruptCoordinator

    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([resp_response(
        resp_function_call("c1", "complete_step",
                           '{"status": "Success", "note": "ok"}'),
    )], stream_events=3)
    coord = InterruptCoordinator()
    # begin_request_watchdog snapshots request_active True just before each await
    # for the next stream event; end/body pulses snapshot False.
    snaps: list[bool] = []
    coord.add_activity_listener(lambda: snaps.append(coord.request_active))

    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    inv = dataclasses.replace(_inv(tmp_path), coordinator=coord)
    events = [e async for e in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"
    # request_active is armed while awaiting each of the 3 streamed events.
    assert sum(1 for s in snaps if s) >= 3
    assert coord.request_active is False


@pytest.mark.anyio
async def test_continuation_resumes_then_completes(tmp_path: Path) -> None:
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(resp_message("prose, no tool call")),
        resp_response(
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log),
                  continuation_fn=lambda _last: ("please call complete_step", False))
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"


@pytest.mark.anyio
async def test_create_passes_stable_prompt_cache_key(tmp_path: Path) -> None:
    """Every create() call within one invocation passes the same
    prompt_cache_key, equal to f"{quest_id}:{agent_id}"."""
    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("u1", "read_file", '{"path": "/project/data.txt"}'),
        ),
        resp_response(
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'),
        ),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path)):
        pass

    calls = fake.responses.calls
    assert len(calls) == 2, f"expected 2 create() calls, got {len(calls)}"
    keys = [c["prompt_cache_key"] for c in calls]
    assert keys == ["test:step:p/s", "test:step:p/s"]


@pytest.mark.anyio
async def test_prompt_cache_key_distinct_per_change(tmp_path: Path) -> None:
    """A different change id yields a different prompt_cache_key, so two
    concurrent changes running the same step don't share a routing slot."""
    from dataclasses import replace

    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'),
        ),
    ])
    inv = _inv(tmp_path)
    inv = replace(
        inv,
        change_context=ChangeContext(
            id="test-2", title="Test 2", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
    )
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(inv):
        pass

    assert fake.responses.calls[0]["prompt_cache_key"] == "test-2:step:p/s"


def test_prompt_cache_key_passthrough_at_boundary() -> None:
    """A key whose full "slug:aid" is exactly 64 chars passes through verbatim."""
    from imp.providers.openai import _prompt_cache_key

    aid = "step:p/s"               # 8 chars
    slug = "a" * 55                 # 55 + 1 (":") + 8 = 64
    key = _prompt_cache_key(slug, aid)
    assert key == f"{slug}:{aid}"
    assert len(key) == 64


def test_prompt_cache_key_hashed_just_over_boundary() -> None:
    """One char over the limit triggers hashing; output is exactly 64 chars."""
    from imp.providers.openai import _prompt_cache_key

    aid = "step:p/s"               # 8 chars
    slug = "a" * 56                 # 56 + 1 + 8 = 65 > 64
    key = _prompt_cache_key(slug, aid)
    assert key != f"{slug}:{aid}"
    assert len(key) == 64


def test_prompt_cache_key_far_over_limit_is_bounded() -> None:
    """A wildly over-limit slug still yields a <= 64-char key."""
    from imp.providers.openai import _prompt_cache_key

    key = _prompt_cache_key("a" * 200, "step:p/s")
    assert len(key) <= 64


def test_prompt_cache_key_deterministic() -> None:
    """Same (slug, aid) always yields the same key (stable turn-over-turn)."""
    from imp.providers.openai import _prompt_cache_key

    slug, aid = "s" * 60, "step:p/s"
    assert _prompt_cache_key(slug, aid) == _prompt_cache_key(slug, aid)


def test_prompt_cache_key_keeps_full_aid_for_long_slug() -> None:
    """For a long slug with a short aid, the whole aid stays visible in the key."""
    from imp.providers.openai import _prompt_cache_key

    aid = "step:code-reviewer/context"   # 26 chars, well under the 54-char budget
    key = _prompt_cache_key("s" * 80, aid)
    assert len(key) == 64
    assert aid in key


def test_prompt_cache_key_distinct_per_aid_when_slug_head_collides() -> None:
    """Two agents in one long-slug change get distinct keys even when the
    slug head (the readable prefix) is identical — the exact case plain
    truncation collapses."""
    from imp.providers.openai import _prompt_cache_key

    slug = "s" * 60                 # slug alone exceeds the 55-char budget
    k1 = _prompt_cache_key(slug, "step:foo/a")
    k2 = _prompt_cache_key(slug, "step:bar/b")
    assert k1 != k2


def test_prompt_cache_key_distinct_per_change_when_slug_head_collides() -> None:
    """Two changes whose long slugs share a 46-char head still get distinct
    keys (distinctness comes from the digest of the full slug:aid)."""
    from imp.providers.openai import _prompt_cache_key

    aid = "step:p/s"
    slug1 = "s" * 55 + "aaaa"       # identical first 46+ chars...
    slug2 = "s" * 55 + "bbbb"       # ...differ only in the truncated tail
    assert _prompt_cache_key(slug1, aid) != _prompt_cache_key(slug2, aid)


@pytest.mark.anyio
async def test_runner_outer_except_reclassifies_when_cancel_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: object(),
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
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: object(),
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
async def test_request_carries_parallel_tool_calls_true(tmp_path: Path) -> None:
    """§4/AC41: every openai Responses request explicitly enables parallel tool calls."""
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'),
        ),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=log)):
        pass
    only_call = fake.responses.calls[0]
    assert only_call["parallel_tool_calls"] is True



@pytest.mark.anyio
async def test_resume_preserves_tool_output_and_omits_manifest(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="read_file", input={"path": "/project/x"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="GROUNDED"),)))

    fake = FakeResponsesClient([
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake,
    )
    [ev async for ev in runner.run(inv)]

    sent = json.dumps(fake.responses.calls[0]["input"])
    assert "GROUNDED" in sent
    assert "replay artifact" not in sent


def test_openai_has_no_wait_for_user_result_handling() -> None:
    """No wait_for_user result handling; the _build_tools carve-out is removed."""
    import inspect

    import imp.providers.openai as openai_mod

    src = inspect.getsource(openai_mod)
    assert '_tu.name == "wait_for_user"' not in src
    assert 'if name == "wait_for_user"' not in src


@pytest.mark.anyio
async def test_openai_llm_response_records_served_model(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
            model="gpt-served-fallback"),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake)
    async for _ev in runner.run(_inv(tmp_path, event_log=log)):
        pass

    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert resp["payload"]["model"] == "gpt-served-fallback"      # served, not configured
    assert "duration_ms" in resp["payload"]
    assert resp["payload"]["duration_ms"] >= 0


@pytest.mark.anyio
async def test_openai_llm_response_records_request_id(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    r = resp_response(
        resp_message("done"),
        resp_function_call("c1", "complete_step", '{"status": "Success"}'))
    r._request_id = "req_openai_xyz"
    fake = FakeResponsesClient([r])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake)
    async for _ev in runner.run(_inv(tmp_path, event_log=log)):
        pass

    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert resp["payload"]["request_id"] == "req_openai_xyz"


@pytest.mark.anyio
async def test_llm_response_carries_context_tokens(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeResponsesClient([
        resp_response(
            resp_message("calling complete_step"),
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'),
            usage={"input_tokens": 1234, "output_tokens": 99,
                   "total_tokens": 1333},
        ),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=log)):
        pass
    resp = next(e for e in log.read() if e["type"] == "llm_response")
    # No cache fields, so the canonical value is input_tokens (not total_tokens).
    assert resp["payload"]["context_tokens"] == 1234


@pytest.mark.anyio
async def test_subagent_bare_turn_end_returns_text(tmp_path: Path) -> None:
    from dataclasses import replace
    fake = FakeResponsesClient([resp_response(resp_message("the final conclusion"))])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log), subagent_depth=1)
    events = [e async for e in runner.run(inv)]
    outs = [e for e in events if e["type"] == "agent_output"]
    assert outs and outs[-1]["payload"] == {"text": "the final conclusion"}
    assert not any(e["type"] == "error" for e in events)


@pytest.mark.anyio
async def test_subagent_bare_turn_end_resume_returns_deliverable(
        tmp_path: Path) -> None:
    """Subagent resume parity with anthropic: a bare-turn-end terminal re-emits
    its final text as the deliverable instead of soliciting the human."""
    from dataclasses import replace
    inv_dir = tmp_path / "step"
    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="go"),)))
    transcript.append(Message(
        role="assistant", content=(TextBlock(text="the conclusion"),)))

    fake = FakeResponsesClient([])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    inv = replace(_inv(tmp_path, event_log=log), subagent_depth=1)

    yielded = [ev async for ev in runner.run(inv)]
    assert [e["type"] for e in control_events(yielded)] == ["agent_output"]
    assert yielded[-1]["payload"] == {"text": "the conclusion"}
    assert not any(e["type"] == "llm_response" for e in log.read())


@pytest.mark.anyio
async def test_bare_turn_end_retry_continue_uses_engine_continuation_not_user_message(
    tmp_path: Path,
) -> None:
    """openai retry-continue on a bare turn-end: no awaiting_user_input, no
    user_message; the nudge is an internal engine_continuation event."""
    from dataclasses import replace

    from imp.agents.transcript import Transcript
    from imp.engine.agent_run import _CONTINUATION_NUDGE

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="what color?"),)))

    fake = FakeResponsesClient([
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])

    def _must_not_solicit(_prompt):
        raise AssertionError("retry-continue must not solicit the human")

    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake,
        user_input_fn=_must_not_solicit)

    async for _ in runner.run(
        replace(inv, event_log=log, balance_on_resume=True,
                resume_should_continue=True)
    ):
        pass

    types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" not in types
    assert "user_message" not in types
    assert "engine_continuation" in types
    cont = next(e for e in log.read() if e["type"] == "engine_continuation")
    assert cont["payload"] == {"text": _CONTINUATION_NUDGE}
    loaded = t.load()
    assert loaded[2].role == "user"
    assert loaded[2].content[0].text == _CONTINUATION_NUDGE
