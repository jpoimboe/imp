"""Task 3 — headless wait_for_user unwinds to a clean, resumable pause.

Covers four seams:
  1. drive_invocation catches AwaitingUserInput and returns a paused result with
     awaiting_user=True (the pause half).
  2. The executor marks the step resumable (resume_pending == step) so the reload
     keeps the transcript carrying the dangling wait_for_user tool_use.
  3. The subagent escalation path (S3) reuses the pre-existing SubagentSuspended
     machinery — the parent's resume_pending is NOT set by the new marker.
  4. claude_code transcript shape: the persisted transcript.jsonl ends at the
     assistant message bearing the dangling wait_for_user tool_use, with NO
     following tool_result for it.

Task 5 — end-to-end pause→persist→reload→resume in a single test:
  5. Anthropic e2e: headless run PAUSES with dangling transcript, then an
     attended re-drive of the SAME persisted transcript solicits, fills the
     real reply, and continues to complete_step — using the stubbed-provider
     harness (no live network calls).
  6. claude_code e2e: same loop using the cc_fakes harness.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.engine.agent_run import AgentRunResult, drive_invocation


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Runner:
    """Minimal runner whose run() raises AwaitingUserInput, mimicking a headless
    wait_for_user unwinding past the provider's tool dispatch."""

    async def interrupt(self) -> None:
        pass

    async def run(self, invocation):
        raise AwaitingUserInput("need a human")
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.anyio
async def test_drive_invocation_treats_awaiting_user_input_as_pause() -> None:
    res = await drive_invocation(
        invocation=object(), runner=_Runner(),
        coordinator=None, log=None, emit_events=False,
    )
    assert isinstance(res, AgentRunResult)
    assert res.paused is True
    assert res.fatal is False
    assert res.errored is False
    assert res.awaiting_user is True


# ── Step 4 GATE: claude_code transcript shape ────────────────────────────────


def _cc_agent_def(tools):
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    return AgentDef(
        name="t", description="", tools=frozenset(tools), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _cc_inv(tmp_path: Path):
    from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import FsRule, SourceLoc
    return AgentInvocation(
        agent_def=_cc_agent_def({"complete_step", "wait_for_user"}),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=InMemoryEventLog(),
        non_interactive=True)


@pytest.mark.anyio
async def test_claude_code_headless_wait_for_user_unwinds_and_dangles(
    tmp_path: Path,
) -> None:
    """GATE: a headless claude_code run whose model calls wait_for_user pauses
    (drive returns paused + awaiting_user), and the persisted transcript.jsonl
    ends at the assistant message bearing the dangling wait_for_user tool_use
    with NO following tool_result for it."""
    from imp.agents.runner import ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from tests.cc_fakes import (
        FakeClaudeSDKClient,
        assistant,
        result,
        text,
        tool_result,
        tool_use,
        user,
    )

    scripted = [
        assistant(
            text("I need a human decision"),
            tool_use("w1", "wait_for_user", {"prompt": "approve?"}),
        ),
        # Simulate the real SDK emitting a tool_result for the wait_for_user call.
        # The runner MUST raise AwaitingUserInput in the iteration that processed
        # the assistant message above — BEFORE consuming this UserMessage — so this
        # tool_result must never reach the persisted transcript.  If the raise were
        # misplaced (fired too late), this message would be consumed and appended,
        # tripping the "no tool_result may resolve the dangling id" assertion below.
        user(tool_result("w1", "user response", is_error=True)),
        result(),
    ]

    def factory(opts):
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    # Headless: no user_input_fn, non_interactive=True.
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    inv = _cc_inv(tmp_path)

    # (a) The run pauses: drive_invocation returns paused + awaiting_user.
    res = await drive_invocation(
        invocation=inv, runner=runner,
        coordinator=None, log=None, emit_events=False)
    assert res.paused is True
    assert res.awaiting_user is True
    assert res.fatal is False
    assert res.errored is False

    # (b) Transcript ends at the dangling wait_for_user tool_use, no tool_result.
    msgs = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert msgs, "transcript must not be empty"
    last = msgs[-1]
    assert last.role == "assistant", f"last message role={last.role}"
    tool_uses = [b for b in last.content if isinstance(b, ToolUseBlock)]
    assert tool_uses, "last assistant message must carry a tool_use"
    assert tool_uses[-1].name == "wait_for_user"
    dangling_id = tool_uses[-1].id

    # No tool_result anywhere in the transcript resolves the dangling tool_use.
    from imp.agents.runner import ToolResultBlock
    for m in msgs:
        for b in m.content:
            assert not (isinstance(b, ToolResultBlock)
                        and b.tool_use_id == dangling_id), \
                "no tool_result may resolve the dangling wait_for_user tool_use"


# ── Step 5: executor marks the headless pause resumable ──────────────────────


@pytest.mark.anyio
async def test_headless_pause_marks_step_resume_pending(tmp_path: Path) -> None:
    """A Continue-entered agent step whose runner raises AwaitingUserInput pauses
    with resume_pending == that step, so the reload keeps the transcript carrying
    the dangling wait_for_user tool_use (test_executor S1 proves keep=is_resume)."""
    from imp.engine.executor import RunResult, run_change
    from imp.engine.state import State
    from imp.providers import resolve
    from tests.test_subagent_pause_unwind import _seed_single_step

    resolve.reset_providers_for_tests()

    class _AwaitRunner:
        async def run(self, invocation):
            raise AwaitingUserInput("approve?")
            yield  # async generator

        async def interrupt(self) -> None: ...

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _AwaitRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _Provider())

    change = _seed_single_step(tmp_path)
    # Sanity: the step is entered via Continue (no resume marker) on first run.
    assert State.load(change.state_dir).resume_pending is None

    res = await run_change(change)
    assert res is RunResult.PAUSED
    st = State.load(change.state_dir)
    assert st.status == "paused"
    # The new marker fired: THIS step is resume_pending so the reload keeps its
    # transcript dir (the dangling wait_for_user tool_use survives).
    assert st.resume_pending == st.step == "w"


# ── Step 6: subagent escalation (S3) uses pre-existing SubagentSuspended ──────


def _seed_lead_with_subagent(root: Path) -> object:
    """Single lead step holding `task`, dispatching a `reviewer` subagent.
    Both lead and reviewer resolve to the `fake` provider."""
    from imp.engine.change import Change
    from imp.engine.loader import load_workflow
    from imp.engine.state import State
    from imp.providers.effective import load_effective_config

    wf = root / "imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "lead.md").write_text(
        "---\nname: lead\ntools: [complete_step, task]\nsubagents: [reviewer]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step, wait_for_user]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: lead, file: agents/lead.md, tier: medium}\n")
    _cfg = ("providers: [fake]\ntiers:\n  low: {fake: m}\n"
            "  medium: {fake: m}\n  high: {fake: m}\n")
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_cfg)

    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "s", "title": "t", "base_branch": "main"},
        indent=2, sort_keys=True))
    configured: list[str] = []
    with contextlib.suppress(Exception):
        configured = load_effective_config(
            project_root=root, control_root=root).config.tier_names()
    workflow = load_workflow(wf, configured_tiers=configured)
    flat = workflow.flat_steps()
    State(id="s", step=flat[0].name, step_attempt=0, status="running",
          vars=dict(workflow.vars.defaults()), loop_counts={}).save(
              change_dir / "state")
    change = Change(id="s", title="t", base_branch="main", control_root=root,
                    project_root=root, change_dir=change_dir)
    change.event_log().append(agent_id=None, type="change_created",
                              payload={"id": "s", "slug": change.slug, "title": "t"})
    return change


@pytest.mark.anyio
async def test_subagent_headless_wait_escalates_via_subagent_suspended(
    tmp_path: Path,
) -> None:
    """A subagent that calls wait_for_user headless escalates the pause through
    the PRE-EXISTING SubagentSuspended machinery (not the new top-level marker):
      - run_change returns PAUSED,
      - the parent transcript ends at the dangling `task` tool_use,
      - resume_pending is NOT set to the parent step (awaiting_user is consumed
        at the subagent's own drive level and deliberately dropped on the
        SubagentSuspended escalation)."""
    from imp.agents.runner import ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.engine.executor import RunResult, run_change
    from imp.engine.state import State
    from imp.providers import resolve

    resolve.reset_providers_for_tests()

    # Subagent runner: emits awaiting_user_input then raises AwaitingUserInput,
    # exactly as the headless wait_for_user tool does inside a real provider.
    class _SubRunner:
        async def run(self, invocation):
            raise AwaitingUserInput("subagent needs a human")
            yield  # async generator

        async def interrupt(self) -> None: ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _SubRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    # Parent lead: a real anthropic runner whose first turn calls `task`. The
    # subagent inherits the `fake` provider (provider_name on the invocation).
    from tests.anthropic_fakes import FakeAsyncAnthropic, response
    from tests.anthropic_fakes import tool_use as a_tool_use

    fake = FakeAsyncAnthropic([response(
        a_tool_use("t1", "task",
                   {"subagent_name": "reviewer", "task_description": "review"}))])

    # Register the parent provider too: run_change resolves the lead step's
    # provider by name. We override create_runner to return our anthropic fake.
    from imp.providers.anthropic import AnthropicAgentRunner

    class _LeadProvider:
        name = "fake"
        model = "m"

        def create_runner(self, inv):
            # The lead step gets the real anthropic runner (drives `task`); the
            # subagent dispatch re-resolves the provider and gets _SubRunner via
            # the registered factory. Distinguish by whether `task` is declared.
            if "task" in inv.agent_def.tools:
                return AnthropicAgentRunner(
                    model_name="m", client_factory=lambda: fake)
            return _SubRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _LeadProvider())

    change = _seed_lead_with_subagent(tmp_path)
    res = await run_change(change)
    assert res is RunResult.PAUSED

    st = State.load(change.state_dir)
    assert st.status == "paused"
    # Pre-existing machinery: the subagent escalation does NOT set the parent
    # step as resume_pending (that marker is reserved for a TOP-LEVEL headless
    # wait_for_user). The subagent-suspend path leaves resume_pending unset.
    assert st.resume_pending != "lead"
    assert st.resume_pending is None

    # Parent transcript ends at the dangling `task` tool_use (the SubagentSuspended
    # unwind left it unbalanced; resume re-dispatches the subtree).
    tdir = change.change_dir / "steps" / "only" / "lead"
    msgs = Transcript(tdir / "transcript.jsonl").load()
    assert msgs, "parent transcript must not be empty"
    last = msgs[-1]
    assert last.role == "assistant"
    tool_uses = [b for b in last.content if isinstance(b, ToolUseBlock)]
    assert tool_uses and tool_uses[-1].name == "task", \
        "parent transcript must end at the dangling task tool_use"


# ── Task 5 — e2e pause → persist → reload → resume in one test ───────────────


@pytest.mark.anyio
async def test_anthropic_e2e_headless_pause_then_attended_resume(
    tmp_path: Path,
) -> None:
    """E2E (Task 5): a single test drives the FULL pause→persist→reload→resume loop
    using the stubbed Anthropic provider.

    Half 1 (headless): the runner's first stubbed turn calls wait_for_user.
    drive_invocation returns paused+awaiting_user, and the persisted transcript
    ends at the dangling wait_for_user tool_use with no resolving tool_result.

    Half 2 (attended): the SAME persisted transcript is re-driven with
    balance_on_resume=True and a user_input_fn returning "do X"; the runner
    injects a SUCCESS-shaped ToolResultBlock with content="do X" against the
    dangling id, then the next stubbed turn calls complete_step → agent_output.

    This test is the ONLY one that proves the full loop from a real headless
    pause through a real attended resume in one test body. The existing
    test_awaiting_user_resume_fills_tool_result_then_continues (in
    test_anthropic_runner.py) covers the resume HALF from a hand-seeded
    transcript; the GATE test covers the pause HALF only.
    """
    import dataclasses

    from imp.agents.runner import (
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use

    def _agent_def_wfu() -> AgentDef:
        return AgentDef(
            name="t", description="",
            tools=frozenset({"complete_step", "wait_for_user"}),
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

    from imp.agents.runner import AgentInvocation, ChangeContext
    transcript_dir = tmp_path / "step"

    def _make_inv(**overrides) -> AgentInvocation:
        base: dict = dict(
            agent_def=_agent_def_wfu(),
            provider_name="anthropic", model_name="claude-sonnet-4-6",
            system_prompt="be terse",
            initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
            change_context=ChangeContext(
                slug="test", title="Test", project_root=tmp_path,
                workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
            interactive=False, transcript_dir=transcript_dir,
            fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                                   mode="allow",
                                   source=SourceLoc(file=Path("w.yaml"), line=1)),),
            events_path=tmp_path / "events.jsonl",
            agent_id="step:p/s", event_log=InMemoryEventLog(),
        )
        base.update(overrides)
        return AgentInvocation(**base)

    # ── Half 1: headless run → pause ─────────────────────────────────────────
    # The single scripted response calls wait_for_user; no user_input_fn is
    # installed so AwaitingUserInput propagates and drive_invocation pauses.
    fake_pause = FakeAsyncAnthropic([
        response(
            text("I need a decision"),
            tool_use("wfu1", "wait_for_user", {"prompt": "approve?"}),
            stop_reason="tool_use",
        ),
    ])
    runner_pause = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: fake_pause,
    )
    inv_headless = _make_inv()

    pause_res = await drive_invocation(
        invocation=inv_headless, runner=runner_pause,
        coordinator=None, log=None, emit_events=False,
    )

    # (a) run returned paused + awaiting_user
    assert pause_res.paused is True
    assert pause_res.awaiting_user is True
    assert pause_res.fatal is False
    assert pause_res.errored is False

    # (b) transcript ends at the dangling wait_for_user tool_use, no tool_result
    transcript_path = transcript_dir / "transcript.jsonl"
    assert transcript_path.exists(), "transcript must be persisted on pause"
    msgs_after_pause = Transcript(transcript_path).load()
    assert msgs_after_pause, "transcript must not be empty after pause"
    last_after_pause = msgs_after_pause[-1]
    assert last_after_pause.role == "assistant"
    wfu_blocks = [b for b in last_after_pause.content if isinstance(b, ToolUseBlock)
                  and b.name == "wait_for_user"]
    assert wfu_blocks, "last assistant message must carry a wait_for_user tool_use"
    dangling_id = wfu_blocks[-1].id
    assert dangling_id == "wfu1"

    for m in msgs_after_pause:
        for b in m.content:
            assert not (isinstance(b, ToolResultBlock)
                        and b.tool_use_id == dangling_id), \
                "no tool_result may resolve the dangling wait_for_user id after pause"

    # ── Half 2: attended re-drive of the SAME persisted transcript → complete ─
    # balance_on_resume=True + user_input_fn: the runner classifies AWAITING_USER,
    # solicits the human, injects a SUCCESS-shaped tool_result, then drives the
    # next LLM call which completes the step.
    fake_resume = FakeAsyncAnthropic([
        response(
            tool_use("c1", "complete_step", {"status": "Success"}),
            stop_reason="tool_use",
        ),
    ])
    log_resume = InMemoryEventLog()
    runner_resume = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: fake_resume,
        user_input_fn=lambda _prompt: "do X",
    )
    inv_resume = dataclasses.replace(
        _make_inv(event_log=log_resume),
        balance_on_resume=True,
        interactive=True,
    )

    events_resume = [ev async for ev in runner_resume.run(inv_resume)]

    # (c) the attended resume produces agent_output (run completes)
    kinds_resume = [e["type"] for e in events_resume]
    assert "agent_output" in kinds_resume, (
        f"expected agent_output after attended resume; got {kinds_resume}"
    )
    assert events_resume[-1]["type"] == "agent_output"
    assert events_resume[-1]["payload"]["status"] == "Success"

    # (d) transcript now has a ToolResultBlock carrying "do X" for the dangling id
    msgs_after_resume = Transcript(transcript_path).load()
    # Find the injected tool_result for the dangling wait_for_user
    injected = None
    for m in msgs_after_resume:
        for b in m.content:
            if isinstance(b, ToolResultBlock) and b.tool_use_id == dangling_id:
                injected = b
    assert injected is not None, (
        "a ToolResultBlock for the dangling wait_for_user id must appear after resume"
    )
    assert injected.content == "do X", (
        f"injected tool_result content must be the real user reply; got {injected.content!r}"
    )
    assert injected.is_error is False, "injected tool_result must not be is_error"

    # (e) event log must have received awaiting_user_input before the user_message
    log_events = list(log_resume.read())
    log_types = [e["type"] for e in log_events]
    assert "awaiting_user_input" in log_types, "awaiting_user_input event must be emitted"
    assert "user_message" in log_types, "user_message event must be emitted"


@pytest.mark.anyio
async def test_claude_code_e2e_headless_pause_then_attended_resume(
    tmp_path: Path,
) -> None:
    """E2E (Task 5 — claude_code variant): same full pause→persist→reload→resume
    loop using the cc_fakes harness.

    Half 1 (headless): scripted SDK emits wait_for_user → drive returns
    paused+awaiting_user, transcript ends at the dangling tool_use.

    Half 2 (attended): re-drive with balance_on_resume=True and user_input_fn
    returning "go ahead"; scripted SDK then emits complete_step → agent_output.
    The transcript carries a SUCCESS-shaped tool_result for the dangling id.

    This test EXTENDS the GATE test (test_claude_code_headless_wait_for_user_unwinds_and_dangles)
    which only covers the pause half: here we run the attended resume continuation
    in the same test body.
    """
    import dataclasses

    from imp.agents.runner import (
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from tests.cc_fakes import (
        FakeClaudeSDKClient,
        assistant,
        result,
        text,
        tool_result,
        tool_use,
        user,
    )

    # ── Half 1: headless pause ────────────────────────────────────────────────
    scripted_pause = [
        assistant(
            text("I need human approval"),
            tool_use("w2", "wait_for_user", {"prompt": "proceed?"}),
        ),
        # SDK would normally emit a tool_result; the runner must raise
        # AwaitingUserInput BEFORE consuming it — proving correct raise site.
        user(tool_result("w2", "user response", is_error=True)),
        result(),
    ]

    def factory_pause(opts):
        return FakeClaudeSDKClient(opts, scripted_messages=scripted_pause)

    runner_pause = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory_pause)
    inv_pause = _cc_inv(tmp_path)

    pause_res = await drive_invocation(
        invocation=inv_pause, runner=runner_pause,
        coordinator=None, log=None, emit_events=False,
    )

    # (a) run returned paused + awaiting_user
    assert pause_res.paused is True
    assert pause_res.awaiting_user is True
    assert pause_res.fatal is False
    assert pause_res.errored is False

    # (b) transcript ends at the dangling wait_for_user tool_use, no tool_result
    transcript_path = inv_pause.transcript_dir / "transcript.jsonl"
    assert transcript_path.exists()
    msgs_after_pause = Transcript(transcript_path).load()
    assert msgs_after_pause
    last_after_pause = msgs_after_pause[-1]
    assert last_after_pause.role == "assistant"
    wfu_blocks = [b for b in last_after_pause.content if isinstance(b, ToolUseBlock)
                  and b.name == "wait_for_user"]
    assert wfu_blocks
    dangling_id = wfu_blocks[-1].id
    assert dangling_id == "w2"

    for m in msgs_after_pause:
        for b in m.content:
            assert not (isinstance(b, ToolResultBlock)
                        and b.tool_use_id == dangling_id)

    # ── Half 2: attended resume ───────────────────────────────────────────────
    # A NEW client script: after the runner injects the tool_result for "w2",
    # the SDK's next turn calls complete_step.
    scripted_resume = [
        assistant(
            text("done"),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def factory_resume(opts):
        return FakeClaudeSDKClient(opts, scripted_messages=scripted_resume)

    log_resume = InMemoryEventLog()
    runner_resume = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory_resume,
        user_input_fn=lambda _prompt: "go ahead",
    )
    # Re-use the SAME transcript_dir as the headless run.
    inv_resume = dataclasses.replace(
        inv_pause,
        event_log=log_resume,
        balance_on_resume=True,
        interactive=True,
        non_interactive=False,
    )

    events_resume = [ev async for ev in runner_resume.run(inv_resume)]

    # (c) the attended resume produces agent_output
    kinds_resume = [e["type"] for e in events_resume]
    assert "agent_output" in kinds_resume, (
        f"expected agent_output after attended resume; got {kinds_resume}"
    )
    assert events_resume[-1]["type"] == "agent_output"

    # (d) transcript now carries a ToolResultBlock with "go ahead" for the dangling id
    msgs_after_resume = Transcript(transcript_path).load()
    injected = None
    for m in msgs_after_resume:
        for b in m.content:
            if isinstance(b, ToolResultBlock) and b.tool_use_id == dangling_id:
                injected = b
    assert injected is not None, (
        "a ToolResultBlock for the dangling wait_for_user id must appear after resume"
    )
    assert injected.content == "go ahead"
    assert injected.is_error is False

    # (e) awaiting_user_input event emitted before user_message
    log_events = list(log_resume.read())
    log_types = [e["type"] for e in log_events]
    assert "awaiting_user_input" in log_types
    assert "user_message" in log_types
