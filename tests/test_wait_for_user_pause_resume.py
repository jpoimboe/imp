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
