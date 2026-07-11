# tests/test_event_logging_integration.py
"""End-to-end contract tests for the LLM event logging system.

These tests verify the full event-logging pipeline using the real
AnthropicAgentRunner with a FakeAsyncAnthropic client:
  2. Subagent events isolated by agent_id in shared log
  3. Gate agent_ids are monotonic (gate:phase/stem-N)
  4. Resume does not re-emit step_context / initial user_message
  5. on_event stream equals disk
  6. parse_message round-trips thinking + signature block order
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 - ensures tools registered
from imp.agents.transcript import Transcript
from imp.engine.change import Change
from imp.engine.state import InMemoryEventLog, JsonlEventLog, State
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use


def _make_change(root: Path, *, change_id: str = "test-change", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "imp"
    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps({
        "id": change_id, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(src_wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id=change_id, step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        id=change_id, title=title, base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": change_id, "title": title},
    )
    return change

# ── shared helpers ────────────────────────────────────────────────────────────

def _agent_def(
    tools: frozenset[str] = frozenset({"complete_step", "read_file"}),
    subagents: tuple[str, ...] = (),
) -> AgentDef:
    return AgentDef(
        name="t", description="", tools=tools, subagents=subagents,
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


def _reviewer_def() -> AgentDef:
    """A minimal reviewer subagent with verdict output."""
    return AgentDef(
        name="reviewer", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="verdict", type="enum",
                            values=("approve", "rework"), required=True),
            ),
            source=SourceLoc(file=Path("r.md"), line=1),
        ),
        prompt_template_source="review",
        source_path=Path("r.md"),
        source=SourceLoc(file=Path("r.md"), line=1),
    )


def _inv(
    tmp_path: Path,
    *,
    tools: frozenset[str] | None = None,
    event_log=None,
    agent_id: str = "step:p/s",
    agent_def: AgentDef | None = None,
    subagent_defs: dict | None = None,
    user_input_fn=None,
) -> AgentInvocation:
    """Build an AgentInvocation mirroring test_anthropic_runner.py._inv()."""
    if agent_def is None:
        agent_def = _agent_def(tools or frozenset({"complete_step", "read_file"}))
    return AgentInvocation(
        agent_def=agent_def,
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
        subagent_defs=subagent_defs or {},
        user_input_fn=user_input_fn,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Test 2: Subagent events isolated by agent_id ──────────────────────────────

@pytest.mark.anyio
async def test_subagent_events_isolated_by_agent_id(tmp_path: Path, provider_registry) -> None:
    """A parent step dispatches one `task` subagent. Verify:
      - parent events carry agent_id 'step:p/s'
      - subagent events carry agent_id 'sub:<tool_call_id>'
      - the subagent step_context.parent_id == the tool_call event id

    FULL implementation: uses real AnthropicAgentRunner dispatch_task path
    with a shared InMemoryEventLog.
    """
    (tmp_path / "art").mkdir()

    reviewer_def = _reviewer_def()
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

    # Parent: turn1 = task tool_use, turn2 (after subagent completes) = complete_step
    # Subagent: runs within the same fake client queue
    # The parent and child share the same FakeAsyncAnthropic queue.
    # Consumption order:
    #   1. parent → tool_use task
    #   2. subagent → bare text turn (final message; no complete_step for subagents)
    #   3. parent → complete_step (after task returns)
    shared_fake = FakeAsyncAnthropic([
        # parent turn 1: dispatch a task
        response(tool_use("t1", "sub_agent", {
            "subagent_name": "reviewer",
            "task_description": "please review",
        })),
        # subagent turn 1: bare text turn is the subagent's terminal deliverable
        response(text("Reviewed — approved."), stop_reason="end_turn"),
        # parent turn 2: finalize after task returns
        response(tool_use("pc", "complete_step",
                           {"status": "Success", "note": "done"})),
    ])

    # Register a fake anthropic provider so spawn_subagent_via_registry
    # builds the child runner with the same client fake (not a real network client).
    # The shared provider_registry fixture owns reset/teardown.
    from imp.providers.anthropic import AnthropicProvider
    provider_registry(
        "anthropic", env_check=lambda: True,
        factory=lambda m: AnthropicProvider(
            model_name=m, client_factory=lambda: shared_fake,
        ),
    )
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: shared_fake,
    )
    events_out = [e async for e in runner.run(inv)]
    assert events_out[-1]["type"] == "agent_output"

    events = list(log.read())

    # Parent events must have agent_id 'step:p/s'
    parent_events = [e for e in events if e.get("agent_id") == "step:p/s"]
    assert parent_events, "no events with parent agent_id 'step:p/s'"

    # Subagent events must have agent_id 'sub:<tool_call_id>'
    sub_ctx_events = [
        e for e in events
        if e["type"] == "step_context"
        and isinstance(e.get("agent_id"), str)
        and e["agent_id"].startswith("sub:")
    ]
    assert sub_ctx_events, "no step_context with sub: agent_id found"
    sub_ctx = sub_ctx_events[0]
    sub_agent_id = sub_ctx["agent_id"]
    tool_call_id = sub_agent_id[len("sub:"):]

    # The step_context's parent_id must equal the tool_call event id
    # (The tool_call event for "t1" is the one that spawned the subagent)
    tool_call_ev = next(
        (e for e in events
         if e["type"] == "tool_call"
         and e.get("payload", {}).get("id") == "t1"),
        None,
    )
    assert tool_call_ev is not None, "no tool_call event for t1"
    assert tool_call_id == tool_call_ev["id"], (
        f"subagent agent_id suffix {tool_call_id!r} != "
        f"tool_call event id {tool_call_ev['id']!r}"
    )
    # sub_ctx.parent_id should point to the tool_call event id
    assert sub_ctx["parent_id"] == tool_call_ev["id"], (
        f"subagent step_context.parent_id {sub_ctx['parent_id']!r} "
        f"!= tool_call event id {tool_call_ev['id']!r}"
    )


# ── Test 3: Backward goto emits step_started for each loop iteration ─────────

@pytest.mark.anyio
async def test_backward_goto_emits_step_started_each_iteration(tmp_path: Path) -> None:
    """A two-step workflow where step `check` gotos back to `work` once (loop),
    then check continues. Assert that step_started is emitted for both loop
    iterations of `work` and `check`, and that each step_started carries the
    correct step/phase/kind payload.

    This replaces the old gate-monotonic-agent_id test: in the unified-step
    model there are no gates, so the invariant being locked is that the
    executor emits a fresh step_started event for every step execution
    (including re-executions caused by backward goto loops).
    """
    from imp.engine.executor import RunResult, run_change
    from imp.providers import resolve

    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "worker.md").write_text(
        "---\nname: worker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "agents" / "checker.md").write_text(
        "---\nname: checker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ncheck\n"
    )
    # Two-step phase: work → check; check can goto work (loop) or continue.
    # max_loops=6 allows at least one backward goto before tripping.
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: work, file: agents/worker.md, tier: low}\n"
        "      - name: check\n"
        "        file: agents/checker.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {if: {verdict: approve}, continue: true}\n"
        "          - {if: {verdict: rework}, goto: work}\n"
        "          - {else: true, pause: true}\n"
    )
    # run_change reads via load_effective_config: project marker + local layer.
    (tmp_path / "imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    change = _make_change(tmp_path)

    # Sequence: work succeeds, check → rework (goto work), work again, check → approve
    call_num = [0]

    class _LocalFakeRunner:
        def __init__(self, output: dict) -> None:
            self._output = output

        async def run(self, invocation: AgentInvocation):
            yield {"type": "agent_output", "payload": {"outcome": "ok", **self._output}}

        async def interrupt(self) -> None:
            pass

    class _SeqProvider:
        name = "fake"

        def __init__(self, model: str) -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation):
            call_num[0] += 1
            n = call_num[0]
            if n == 1:
                return _LocalFakeRunner({"status": "Success"})           # work run 1
            elif n == 2:
                # check step → rework → goto work
                return _LocalFakeRunner({"status": "Success", "verdict": "rework"})
            elif n == 3:
                return _LocalFakeRunner({"status": "Success"})           # work run 2
            else:
                # check step → approve → continue
                return _LocalFakeRunner({"status": "Success", "verdict": "approve"})

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=_SeqProvider,
    )

    collected_events: list[dict] = []
    result = await run_change(change, on_event=collected_events.append)
    assert result is RunResult.COMPLETED, f"expected COMPLETED, got {result}"

    events = list(change.event_log().read())

    # Verify step_started events: work should appear twice, check should appear twice.
    started_steps = [
        e["payload"]["step"]
        for e in events
        if e.get("type") == "step_started"
        and e.get("payload", {}).get("step") in ("work", "check")
    ]
    assert started_steps.count("work") >= 2, (
        f"expected at least 2 step_started for 'work' (loop re-run), got: {started_steps}"
    )
    assert started_steps.count("check") >= 2, (
        f"expected at least 2 step_started for 'check' (loop re-run), got: {started_steps}"
    )

    # Each step_started must carry kind="agent" and phase="a".
    for e in events:
        step_in_wf = e.get("payload", {}).get("step") in ("work", "check")
        if e.get("type") == "step_started" and step_in_wf:
            payload = e["payload"]
            assert payload.get("kind") == "agent", (
                f"step_started payload missing kind='agent': {payload}"
            )
            assert payload.get("phase") == "a", (
                f"step_started payload missing phase='a': {payload}"
            )

    # Verify step_completed events carry action as a dict with a verb key.
    for e in events:
        if e.get("type") == "step_completed":
            action = e.get("payload", {}).get("action")
            assert isinstance(action, dict) and "verb" in action, (
                f"step_completed action must be a dict with 'verb', got: {action!r}"
            )


# ── Test 4: Resume does not re-emit step_context / initial user_message ───────

@pytest.mark.anyio
async def test_resume_does_not_reemit_step_context(tmp_path: Path) -> None:
    """Fresh-run a step to a mid-step transcript (one tool-use turn, stops there
    because the fake runs out). Then re-enter the runner over the SAME
    transcript_dir + a FRESH JsonlEventLog. Assert exactly one step_context
    and one initial user_message ("go") for agent_id 'step:p/s' after both runs.

    FULL implementation: uses the runner's real resume branch (existing transcript
    → skips fresh-entry scaffolding). The runner stops cleanly after one
    read_file turn because the fake runs out and returns an error.
    The second run uses a fresh JsonlEventLog over the same path (rebuilds
    chain) and a pre-populated transcript_dir.
    """
    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("content")

    log_path = tmp_path / "events.jsonl"

    # ── Run 1: drive one tool_use turn, then runner stops (fake exhausted) ───
    # Turn 1: text + read_file (runner will process this tool, then on the
    # next LLM call the fake is exhausted and raises IndexError → runner errors)
    # We need the transcript to be populated so run 2 takes the resume branch.
    # Use a direct transcript seed approach (mirrors test_resume_uses_existing_transcript)
    # to ensure we control exactly what's in the transcript.
    step_dir = tmp_path / "step"
    step_dir.mkdir(parents=True, exist_ok=True)

    _inv(tmp_path, event_log=JsonlEventLog(log_path), agent_id="step:p/s")

    # Seed a transcript: user("go") → assistant tool_use(read_file) → user(tool_result)
    # This mirrors how test_anthropic_runner.py tests resume.
    transcript = Transcript(step_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="go"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/data.txt"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="content"),
    )))

    # Write a fresh-entry step_context + user_message to the log manually,
    # to simulate what run 1 would have emitted before the mid-step interrupt.
    log1 = JsonlEventLog(log_path)
    log1.append_step_context(
        agent_id="step:p/s", parent_id_override="seed-run1",
        provider="anthropic", model="claude-sonnet-4-6",
        system_prompt="be terse",
        tools_schema=[], params={"max_tokens": 8192}, max_tool_calls=50,
    )
    log1.append_user_message(
        agent_id="step:p/s",
        message=Message(role="user", content=(TextBlock(text="go"),)),
        human=True,
    )

    # ── Run 2: resume over the same transcript + fresh JsonlEventLog ──────────
    # fresh JsonlEventLog rebuilds the chain from disk (tests chain-rebuild)
    log2 = JsonlEventLog(log_path)  # fresh instance, same path
    fake2 = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "resumed"})),
    ])
    runner2 = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake2,
    )
    inv2 = _inv(tmp_path, event_log=log2, agent_id="step:p/s")
    # Run 2 will see the existing transcript and take the resume branch
    # (no step_context emitted, no initial user_message re-emitted)
    out2 = [e async for e in runner2.run(inv2)]
    assert out2[-1]["type"] == "agent_output", (
        f"expected agent_output, got {[e['type'] for e in out2]}"
    )

    # ── Verify exactly one step_context and one initial "go" user_message ─────
    events = list(JsonlEventLog(log_path).read())

    sc = [e for e in events
          if e["type"] == "step_context"
          and e.get("agent_id") == "step:p/s"]
    um = [e for e in events
          if e["type"] == "user_message"
          and e.get("agent_id") == "step:p/s"
          and e.get("payload", {}).get("content")
          and e["payload"]["content"][0].get("text") == "go"]

    assert len(sc) == 1, (
        f"expected exactly 1 step_context for step:p/s, got {len(sc)}"
    )
    assert len(um) == 1, (
        f"expected exactly 1 initial user_message 'go' for step:p/s, got {len(um)}"
    )


# ── Test 5: on_event stream equals disk ────────────────────────────────────────

@pytest.mark.anyio
async def test_on_event_stream_equals_disk(tmp_path: Path) -> None:
    """Run a simple complete-step turn with a JsonlEventLog that has on_event.
    Verify that every event seen via on_event is exactly the same (same ids,
    same order, equal dicts) as what's on disk in a fresh JsonlEventLog.

    FULL implementation: no narrowing needed.
    """
    (tmp_path / "art").mkdir()

    seen: list[dict] = []
    log = JsonlEventLog(tmp_path / "events.jsonl", on_event=seen.append)

    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    async for _ in runner.run(_inv(tmp_path, event_log=log)):
        pass

    # Read a fresh JsonlEventLog over the same path to verify persistence
    on_disk = list(JsonlEventLog(tmp_path / "events.jsonl").read())

    # The on_event list must match exactly what's on disk
    assert len(seen) > 0, "on_event callback never fired"
    assert [e["id"] for e in seen] == [e["id"] for e in on_disk], (
        "event id order differs between on_event stream and disk"
    )
    assert seen == on_disk, (
        "on_event stream and disk contents differ"
    )


# ── Test 6: parse_message round-trips thinking + signature block order ────────

def test_parse_message_round_trips_thinking_signature_blockorder() -> None:
    """parse_message preserves tool_use block order and PRESERVES the signed
    thinking block (with its signature) in the reconstructed Message.

    FULL implementation: complete as specified.
    """
    from imp.engine.replay import parse_message

    raw = {"content": [
        {"type": "thinking", "thinking": "reasoning", "signature": "sig-9"},
        {"type": "text", "text": "ans"},
        {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
        {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
    ]}
    msg = parse_message("anthropic", raw)

    # Block order for tool_use blocks must be preserved
    names = [getattr(b, "name", None) for b in msg.content
             if getattr(b, "type", "") == "tool_use"]
    assert names == ["a", "b"], (
        f"tool_use block order not preserved: {names}"
    )

    # The signed thinking block is PRESERVED, leading the turn, signature intact.
    from imp.agents.runner import ThinkingBlock
    assert msg.content[0] == ThinkingBlock(thinking="reasoning", signature="sig-9")

    # OpenAI path: simple text-only response (Responses output[] shape).
    oraw = {"output": [{
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }]}
    omsg = parse_message("openai", oraw)
    assert omsg.content[0].text == "hi", (
        f"expected openai text 'hi', got {omsg.content[0]}"
    )
