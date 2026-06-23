# tests/test_claude_code_runner.py
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.claude_code_session_store import (
    ImpSessionStore,
    resume_session_id,
    session_key,
    session_store_for,
)
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    result,
    text,
    thinking,
    tool_use,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def(tools: frozenset[str] = frozenset({"complete_step"})) -> AgentDef:
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


def _inv(
    tmp_path: Path,
    tools: frozenset[str] | None = None,
    event_log: InMemoryEventLog | None = None,
    agent_id: str = "step:p/s",
) -> AgentInvocation:
    log = event_log if event_log is not None else InMemoryEventLog()
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step"})),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id=agent_id,
        event_log=log,
    )


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()

    scripted = [
        assistant(
            text("calling complete_step"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
        # Claude's auto-generated user message carrying the tool_result for c1
        # would normally come back from the MCP server. The fake stops here;
        # the runner intercepts complete_step via state, not via tool_result
        # observation.
        result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=fake_client_factory,
    )
    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    # After the flip: runners yield ONLY agent_output/error
    kinds = [e["type"] for e in events]
    assert kinds == ["agent_output"], f"expected only agent_output, got {kinds}"
    assert events[-1]["payload"] == {"status": "Success", "note": "ok"}

    # Audit trail is in the event log
    log_events = list(log.read())
    log_types = [e["type"] for e in log_events]
    assert "step_context" in log_types
    assert "user_message" in log_types
    assert "llm_response" in log_types

    # All events have the right agent_id
    for e in log_events:
        assert e["agent_id"] == "step:p/s", f"wrong agent_id: {e}"


@pytest.mark.anyio
async def test_cancelled_no_result_yields_interrupted_not_network(
    tmp_path: Path,
) -> None:
    """Ctrl-C tears down the claude subprocess; the re-query's receive_response
    then yields zero messages and the runner falls through the no-result path.
    When the runner was cancelled, that fallthrough MUST report the cancellation
    (config/interrupted -> Pause), not a retryable `network`/no_result_message
    error. A `network` classification auto-retries and replays the interrupted
    transcript, which the API rejects with a 400 ("tool use concurrency issues").

    Regression for change 2026-06-06-06x3ml: Ctrl-C surfaced as a network error
    + retry + 400 pause instead of a clean interrupt pause.
    """
    (tmp_path / "art").mkdir()

    def factory(opts: Any) -> FakeClaudeSDKClient:
        # Dead subprocess: receive_response yields nothing, no ResultMessage.
        return FakeClaudeSDKClient(opts, scripted_messages=[])

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    await runner.interrupt()  # the SIGINT-driven cancel that already fired

    inv = _inv(tmp_path)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, errs
    assert errs[0]["payload"]["category"] == "config", errs[0]["payload"]
    assert errs[0]["payload"]["kind"] == "interrupted", errs[0]["payload"]


@pytest.mark.anyio
async def test_terminated_process_teardown_classified_interrupted_not_unknown(
    tmp_path: Path,
) -> None:
    """A process-group SIGINT can kill a (sub)agent's claude CLI child BEFORE the
    runner's own interrupt()/_cancel fires; the SDK then raises
    `CLIConnectionError: Cannot write to terminated process`. That is interrupt
    teardown, not a provider failure — it must classify as config/interrupted, NOT
    the `unknown` "unclassified provider error" the --tui playbook caught (which
    also surfaces a `paused: unknown` headline). Note: NO runner.interrupt() here,
    so `_cancel` stays False — this exercises the terminated-process arm that
    catches a killed subagent whose own runner never saw the cancel.
    """
    (tmp_path / "art").mkdir()

    class CLIConnectionError(Exception):
        pass

    class _DeadOnQuery(FakeClaudeSDKClient):
        async def query(self, prompt: Any) -> None:
            raise CLIConnectionError(
                "Cannot write to terminated process (exit code: 0)")

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: _DeadOnQuery(opts, scripted_messages=[]),
    )
    inv = _inv(tmp_path)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, errs
    assert errs[0]["payload"]["category"] == "config", errs[0]["payload"]
    assert errs[0]["payload"]["kind"] == "interrupted", errs[0]["payload"]
    # The forbidden classification must NOT appear.
    assert "unclassified provider error" not in errs[0]["payload"]["message"]


@pytest.mark.anyio
async def test_cancel_during_continuation_skips_nudge_and_requery(
    tmp_path: Path,
) -> None:
    """If the interrupt lands while imp is resolving the end-of-turn
    continuation (the complete_step nudge), the runner must pause at the turn
    boundary instead of appending the nudge and re-querying. Skipping the doomed
    re-query is what suppresses the spurious `[network] SDK closed` warning a
    user sees after Ctrl-C.

    Distinguishing signal: exactly ONE query reached the client (the initial
    history replay, no nudge re-query).
    """
    from dataclasses import replace

    (tmp_path / "art").mkdir()

    scripted = [
        assistant(text("thinking about the plan")),  # no complete_step
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )

    def cancelling_cont(_last_text: str) -> str:
        # The Ctrl-C lands here, mid-continuation.
        runner._cancel = True
        return "You ended your turn without calling complete_step"

    inv = replace(_inv(tmp_path), continuation_fn=cancelling_cont)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, errs
    assert errs[0]["payload"]["category"] == "config", errs[0]["payload"]
    assert errs[0]["payload"]["kind"] == "interrupted", errs[0]["payload"]
    # The nudge re-query must NOT have fired: only the initial history replay.
    assert len(created[0].queries) == 1, (
        f"expected no re-query after cancel, got {created[0].queries}")


@pytest.mark.anyio
async def test_resume_from_existing_transcript(tmp_path: Path) -> None:
    """Pre-populate transcript with a user/assistant/user(tool_result) sequence;
    runner skips initial_messages and replays history via client.query."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="thinking…"),
            ToolUseBlock(id="t0", name="read_file", input={"path": "/p/x"}),
        ),
    ))
    t.append(Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t0", content="x"),),
    ))

    scripted = [
        assistant(
            text("done"),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    assert events[-1]["type"] == "agent_output"
    # Initial messages were NOT re-appended.
    loaded = t.load()
    assert len(loaded) == 5  # 3 original + 1 manifest user turn + 1 new assistant turn


@pytest.mark.anyio
async def test_runs_subagent_via_task(tmp_path: Path) -> None:
    """Parent dispatches via task → child runner runs to complete_step →
    parent receives JSON-stringified payload and emits its own complete_step."""

    # NOTE: the fake client bypasses the real MCP transport, so _impl for "task"
    # is not invoked; the race-free tool_call-append path is integration-only
    # (covered in Task 16). This test verifies dispatch wiring + return payload.
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
        prompt_template_source="hi",
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

    parent_scripted = [
        assistant(
            text("dispatching"),
            tool_use("p1", "task",
                     {"subagent_name": "sub", "task_description": "do work"}),
        ),
        # SDK will send a tool_result back from the MCP server's task wrapper.
        # The fake doesn't simulate that round trip; the runner sees the
        # parent's next agent turn directly.
        assistant(
            text("got result"),
            tool_use("p2", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]
    child_scripted = [
        assistant(
            text("sub working"),
            tool_use("s1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    scripts = iter([parent_scripted, child_scripted])

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=next(scripts))

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success"}


@pytest.mark.anyio
async def test_sdk_task_result_echo_is_suppressed_and_flips_is_error(
    tmp_path: Path,
) -> None:
    """Receive-loop suppression branch (claude_code.py, user-role ToolResultBlock
    handling). When the SDK echoes the `task` tool_result back through the receive
    loop, the runner must:
      (a) SUPPRESS that echo — _impl already appended the canonical, minted
          tool_result for the task, so the SDK's `toolu_…` echo must NOT be
          appended a second time (one task → one tool_result event); AND
      (b) STILL flip the task ToolOutcome.is_error to the echoed value, so a
          successful dispatch isn't left at the default is_error=True and tripped
          as a LoopGuard repeated_failure on identical re-dispatches (M1 regress).

    The fake client bypasses MCP, so _impl is not invoked here; this test
    pre-seeds the canonical minted call+result (as _impl would) on a DISTINCT
    block id, then drives the SDK's `toolu_…` echo through the loop and asserts
    suppression + the is_error flip (captured off the LoopGuard's TurnRecord).
    """
    import unittest.mock as _mock

    from imp.agents.runner import ToolResultBlock as RealToolResultBlock
    from imp.agents.runner import ToolUseBlock as RealToolUseBlock
    from imp.providers import claude_code as cc
    from tests.cc_fakes import FakeToolResultBlock, FakeUserMessage

    (tmp_path / "art").mkdir()

    parent_def = _agent_def(tools=frozenset({"complete_step", "task"}))
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    inv = replace(inv, agent_def=parent_def)

    # Simulate _impl having already logged the canonical task call+result on the
    # MINTED block id (distinct from the SDK's toolu_… id-space).
    minted_id = "minted-deadbeef"
    sdk_id = "toolu_sdk_echo"
    log.append_tool_call(
        agent_id="step:p/s",
        block=RealToolUseBlock(id=minted_id, name="task",
                               input={"subagent_name": "sub",
                                      "task_description": "do work"}))
    log.append_tool_result(
        agent_id="step:p/s",
        block=RealToolResultBlock(minted_id, "sub done", is_error=False))

    # Drive: assistant emits the `task` tool_use (id = SDK toolu_…); the SDK then
    # echoes a user-role ToolResultBlock for that same id (is_error=False); the
    # parent resumes and completes.
    scripted = [
        assistant(
            text("dispatching"),
            tool_use(sdk_id, "task",
                     {"subagent_name": "sub", "task_description": "do work"}),
        ),
        FakeUserMessage(content=[
            FakeToolResultBlock(tool_use_id=sdk_id, content="sub done",
                                is_error=False),
        ]),
        assistant(
            text("got result"),
            tool_use("p2", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    # Spy on the LoopGuard to capture the TurnRecord carrying the task outcome,
    # so we can read the flipped is_error off the canonical accumulation path.
    observed: list[Any] = []
    real_observe = cc.LoopGuard.observe

    def spy_observe(self: Any, record: Any) -> Any:
        observed.append(record)
        return real_observe(self, record)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    with _mock.patch.object(cc.LoopGuard, "observe", spy_observe):
        events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"

    # (a) Exactly ONE task tool_result in the log: the minted one. The SDK's
    #     toolu_… echo was suppressed (else there would be two).
    task_results = [
        e for e in log.read()
        if e["type"] == "tool_result"
        and e["payload"]["id"] in (minted_id, sdk_id)
    ]
    assert len(task_results) == 1, task_results
    assert task_results[0]["payload"]["id"] == minted_id
    assert not any(e["payload"]["id"] == sdk_id for e in log.read()
                   if e["type"] == "tool_result"), "SDK task echo was NOT suppressed"

    # (b) The task ToolOutcome's is_error was flipped to the echoed value (False),
    #     off the default True — proving the suppression branch still runs the flip.
    task_outcomes = [
        t for rec in observed for t in rec.tools if t.name == "task"
    ]
    assert task_outcomes, "no task ToolOutcome reached the LoopGuard"
    assert all(t.is_error is False for t in task_outcomes), (
        "task is_error not flipped to the echoed False (stuck at default True)")


@pytest.mark.anyio
async def test_awaiting_user_resume_fills_tool_result_then_continues(
    tmp_path: Path,
) -> None:
    """AWAITING_USER resume: a paused transcript ends on a `wait_for_user`
    tool_use; the runner solicits the human and fills a SUCCESS-shaped
    tool_result (not a TextBlock) before driving the next LLM call."""
    from imp.agents.runner import (
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.transcript import Transcript
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="need a decision"),
            ToolUseBlock(id="wfu1", name="wait_for_user",
                         input={"prompt": "merge?"}),
        ),
    ))

    scripted = [
        assistant(text("done"), tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    user_inputs = iter(["yes, proceed"])
    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory,
        user_input_fn=lambda _prompt: next(user_inputs),
    )

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    # After the flip: user_input is NO LONGER yielded; it's appended to the log.
    # The runner should yield only agent_output.
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "agent_output"

    # Verify ordering in the event log: awaiting_user_input + user_message
    # before llm_response.
    log_events = list(log.read())
    log_types = [e["type"] for e in log_events]
    assert "awaiting_user_input" in log_types
    user_idx = log_types.index("user_message")
    llm_idx = log_types.index("llm_response")
    assert user_idx < llm_idx, (
        f"user_message (idx={user_idx}) must precede llm_response (idx={llm_idx})"
    )

    loaded = t.load()
    assert len(loaded) >= 3
    assert loaded[2].role == "user"
    block = loaded[2].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "wfu1"
    assert block.content == "yes, proceed"
    assert block.is_error is False


@pytest.mark.anyio
async def test_headless_message_does_not_pause_on_first_wait_for_user(
    tmp_path: Path,
) -> None:
    """Regression: under a headless `--message` run the invocation is
    non_interactive=True AND has a one-shot reply user_input_fn attached. The
    runner-level dual-write must treat that as ATTENDED (attended iff a
    user_input_fn exists; non_interactive no longer participates), so it does
    NOT mark awaiting_user on the first wait_for_user. The run consumes the
    reply and proceeds to complete_step rather than pausing immediately.

    Guards the stale `and not invocation.non_interactive` rule that killed
    `--message` on the production claude_code provider."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(
        tmp_path,
        tools=frozenset({"complete_step", "wait_for_user"}),
        event_log=log,
    )
    inv = replace(inv, non_interactive=True)

    scripted = [
        assistant(
            text("need a decision"),
            tool_use("wfu1", "wait_for_user", {"prompt": "merge?"}),
        ),
        assistant(
            text("proceeding"),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory,
        # One-shot reply channel installed by a headless --message run.
        user_input_fn=lambda _prompt: "go ahead",
    )

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    # With the stale rule the runner would have raised AwaitingUserInput on the
    # first wait_for_user (awaiting_user=True) and never reached complete_step.
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "agent_output", f"expected agent_output, got {kinds}"
    assert events[-1]["payload"] == {"status": "Success"}


@pytest.mark.anyio
async def test_no_channel_wait_for_user_marks_pause(tmp_path: Path) -> None:
    """The dual-write's ONLY remaining job: when NO reply channel is attached
    (user_input_fn is None — subagent / no-channel run), the first
    wait_for_user marks the pause and run() unwinds with AwaitingUserInput."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(
        tmp_path,
        tools=frozenset({"complete_step", "wait_for_user"}),
        event_log=log,
    )

    scripted = [
        assistant(
            text("need a decision"),
            tool_use("wfu1", "wait_for_user", {"prompt": "merge?"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory,
        user_input_fn=None,
    )

    with pytest.raises(AwaitingUserInput) as ei:
        async for _ev in runner.run(inv):
            pass
    assert ei.value.prompt == "merge?"


@pytest.mark.anyio
async def test_thinking_block_emits_thinking_event(tmp_path: Path) -> None:
    """Extended-thinking output from claude_code is captured as a `thinking`
    event in the log, not folded into runner_warning."""
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(
            thinking("weighing options..."),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=fake_client_factory,
    )
    events = [ev async for ev in runner.run(inv)]

    # After the flip: thinking is in the log, not yielded
    kinds = [e["type"] for e in events]
    assert "thinking" not in kinds, f"thinking should be in log, not yielded: {kinds}"
    assert kinds == ["agent_output"], f"expected only agent_output, got {kinds}"

    log_events = list(log.read())
    log_types = [e["type"] for e in log_events]
    assert "thinking" in log_types
    thinkings = [e["payload"]["content"] for e in log_events if e["type"] == "thinking"]
    assert thinkings == ["weighing options..."]


def test_classify_agent_text_invalid_api_key() -> None:
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    out = cc._classify_agent_text("Some preamble. Invalid API key, sorry.")
    assert isinstance(out, perr.AuthError)


def test_classify_agent_text_login_required() -> None:
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    out = cc._classify_agent_text("Please run `/login` first.")
    assert isinstance(out, perr.AuthError)


def test_classify_agent_text_quota_usage_limit() -> None:
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    out = cc._classify_agent_text("You have reached your usage limit for today.")
    assert isinstance(out, perr.QuotaError)


def test_classify_agent_text_credit_balance() -> None:
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    out = cc._classify_agent_text("Sorry — Credit balance is too low to continue.")
    assert isinstance(out, perr.QuotaError)


def test_classify_agent_text_benign_returns_none() -> None:
    from imp.providers import claude_code as cc
    assert cc._classify_agent_text("I'm working on the task. One moment.") is None
    # Pattern conservatism: a tool result that *mentions* auth errors must
    # NOT trip the classifier.
    assert cc._classify_agent_text(
        "git push reported: Authentication failed for repo X",
    ) is None


def test_text_of_concatenates_textblocks_only() -> None:
    from imp.agents.runner import Message, TextBlock, ToolUseBlock
    from imp.providers import claude_code as cc

    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="hello "),
            ToolUseBlock(id="t1", name="bash", input={}),
            TextBlock(text="world"),
        ),
    )
    assert cc._text_of(msg) == "hello \nworld"


@pytest.mark.anyio
async def test_runner_reclassifies_invalid_api_key_text_to_auth_event(
    tmp_path: Path,
) -> None:
    """When the claude CLI returns 'Invalid API key' as an assistant
    text response and the SDK closes the stream with a ResultMessage,
    the runner must yield a single category=auth error event (NOT
    missing_complete_step / no_complete_step)."""
    # Build a fake SDK client that yields: AssistantMessage("Invalid API key"),
    # then ResultMessage. The runner should reclassify the assistant text
    # via _classify_agent_text and emit a category=auth event.
    fake_messages = [
        assistant(text("Invalid API key — got 401. Cannot continue.")),
        result(),
    ]

    def client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=fake_messages)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=client_factory,
    )
    inv = _inv(tmp_path)

    events: list[dict] = []
    async for ev in runner.run(inv):
        events.append(ev)

    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1, (
        f"expected 1 error event, got "
        f"{[e.get('payload', {}) for e in error_events]}"
    )
    payload = error_events[0]["payload"]
    assert payload["category"] == "auth", (
        f"expected category=auth, got {payload}"
    )


@pytest.mark.anyio
async def test_continuation_resumes_then_completes(tmp_path: Path) -> None:
    """continuation_fn nudges the agent, which then calls complete_step."""
    (tmp_path / "art").mkdir()

    # Flat script: turn 1 yields prose+result (no complete_step),
    # turn 2 yields complete_step+result after the nudge.
    scripted = [
        assistant(text("prose, no tool")), result(),
        assistant(tool_use("c1", "complete_step", {"status": "Success"})), result(),
    ]

    fake: FakeClaudeSDKClient | None = None

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        nonlocal fake
        fake = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        return fake

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    inv = replace(inv, continuation_fn=lambda _last: "please call complete_step")

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=fake_client_factory,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"

    # Verify the nudge text was actually sent to the CLI via client.query().
    # fake.queries records every string (or drained async-iterable) passed to
    # query(); the continuation nudge is a plain string sent on the resume turn.
    assert fake is not None
    assert "please call complete_step" in fake.queries, (
        f"expected nudge text in fake.queries, got: {fake.queries}"
    )


@pytest.mark.anyio
async def test_continuation_exhaustion_emits_agent_incomplete(tmp_path: Path) -> None:
    """continuation_fn returning None after N nudges yields agent_incomplete."""
    (tmp_path / "art").mkdir()

    # Three turns of DISTINCT prose — agent never calls complete_step. Distinct
    # so the loop_guard's repeated_failure detector (identical-signature x K)
    # doesn't fire; with only 3 turns the no_progress detector (N=6) also stays
    # below threshold, so the nudge-exhaustion path is what terminates the run.
    scripted = [
        assistant(text("prose one")), result(),
        assistant(text("prose two")), result(),
        assistant(text("prose three")), result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    calls: dict[str, int] = {"n": 0}

    def cont(_last: str) -> str | None:
        if calls["n"] >= 2:
            return None
        calls["n"] += 1
        return "please call complete_step"

    inv = replace(inv, continuation_fn=cont)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=fake_client_factory,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "agent_incomplete"
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_classified_auth_text_wins_over_nudge(tmp_path: Path) -> None:
    """Non-interactive agent whose last text matches an auth pattern surfaces
    AuthError; continuation_fn is never consulted."""
    (tmp_path / "art").mkdir()

    scripted = [assistant(text("Invalid API key")), result()]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    consulted: dict[str, bool] = {"hit": False}

    def cont(_last: str) -> str | None:
        consulted["hit"] = True
        return "nudge"

    inv = replace(inv, continuation_fn=cont)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=fake_client_factory,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "auth"
    assert consulted["hit"] is False


def test_fake_receive_response_segments_per_result_message() -> None:
    import anyio

    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text

    scripted = [
        assistant(text("turn one")), result(),
        assistant(text("turn two")), result(),
    ]
    fake = FakeClaudeSDKClient(object(), scripted_messages=scripted)

    async def drain_round() -> list[str]:
        return [type(m).__name__ async for m in fake.receive_response()]

    first = anyio.run(drain_round)
    second = anyio.run(drain_round)
    assert first == ["AssistantMessage", "ResultMessage"]
    assert second == ["AssistantMessage", "ResultMessage"]
    # Exhausted: a third call yields nothing.
    assert anyio.run(drain_round) == []


@pytest.mark.anyio
async def test_requery_across_two_receive_rounds_no_error(tmp_path: Path) -> None:
    """A turn ending without complete_step re-queries via continuation_fn; turn 2
    calls complete_step. With per-turn receive_response() this must NOT surface a
    no_result_message error (issue 7 regression)."""
    (tmp_path / "art").mkdir()

    # Round 1: prose only (a question) + result. Round 2: complete_step + result.
    scripted = [
        assistant(text("Should I proceed?")), result(),
        assistant(tool_use("c1", "complete_step", {"status": "Success"})), result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    inv = replace(inv,
                  continuation_fn=lambda _last: "looks good, proceed")

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=fake_client_factory,
    )
    events = [e async for e in runner.run(inv)]

    kinds = [e["type"] for e in events]
    assert "error" not in kinds, f"unexpected error event(s): {events}"
    assert kinds[-1] == "agent_output"


def test_claude_code_provider_registered_and_resolves() -> None:
    """The claude_code provider is registered eagerly; resolve_run picks it up
    when claude is on PATH."""
    import importlib
    import shutil
    from pathlib import Path

    import imp.providers
    from imp.engine.types import (
        AgentDef,
        OutputField,
        OutputSchema,
        Pause,
        Phase,
        SourceLoc,
        Step,
        Vars,
        Workflow,
        WorkflowDefaults,
    )
    from imp.providers import resolve
    from imp.providers.claude_code import ClaudeCodeProvider
    from imp.providers.config import Config

    resolve.reset_providers_for_tests()
    importlib.reload(imp.providers)

    src = SourceLoc(file=Path("w.yaml"), line=1)
    cfg = Config(
        providers=("claude_code",),
        tiers={
            "low":    {"claude_code": "claude-haiku-4-5"},
            "medium": {"claude_code": "claude-sonnet-4-5"},
            "high":   {"claude_code": "claude-opus-4"},
        },
    )
    agent = AgentDef(
        name="a", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),), source=src),
        prompt_template_source="", source_path=Path("a.md"), source=src)
    step = Step(name="s", phase="p", kind="agent", rules=(),
                on_max_retries=Pause(), max_loops=6, source=src,
                file=Path("agents/a.md"), tier="medium")
    wf = Workflow(
        name="w", description="",
        phases=(Phase(name="p", steps=(step,), source=src),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents={"a": agent}, workflow_dir=Path("."), source=src)

    if shutil.which("claude") is None:
        # Not on PATH — resolve should fail with ProviderUnavailable.
        with pytest.raises(resolve.ProviderUnavailable):
            resolve.resolve_run(wf, cfg)
        return

    resolved_tiers, _, _ = resolve.resolve_run(wf, cfg)
    pm = resolved_tiers["medium"]
    assert pm.provider == "claude_code"
    provider = resolve.provider_from_model(pm)
    assert isinstance(provider, ClaudeCodeProvider)


@pytest.mark.anyio
async def test_runner_translates_unknown_sdk_exception_to_unknown_event(
    tmp_path: Path,
) -> None:
    """An unexpected exception from the SDK must produce a single
    category=unknown error event (not crash the runner)."""
    class _ExplodingClient:
        async def __aenter__(self):
            raise RuntimeError("SDK broke unexpectedly")

        async def __aexit__(self, *args):
            return None

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda _opts: _ExplodingClient(),
    )
    inv = _inv(tmp_path)

    events: list[dict] = []
    async for ev in runner.run(inv):
        events.append(ev)

    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1, (
        f"expected 1 error event, got {[e.get('payload', {}) for e in error_events]}"
    )
    assert error_events[0]["payload"]["category"] == "unknown"


@pytest.mark.anyio
async def test_runner_outer_except_reclassifies_when_cancel_is_set(
    tmp_path: Path,
) -> None:
    """When `_cancel` is True (interrupt fired before the SDK exception
    bubbled), the outer `except Exception` must emit the same `interrupted`
    error event the in-loop check emits — NOT `unclassified provider error`."""
    class _ExplodingClient:
        async def __aenter__(self) -> None:
            raise RuntimeError("Cannot write to terminated process (exit code: 0)")
        async def __aexit__(self, *args: object) -> None:
            return None

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda _opts: _ExplodingClient(),
    )
    runner._cancel = True
    inv = _inv(tmp_path)

    events = [ev async for ev in runner.run(inv)]
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    payload = error_events[0]["payload"]
    assert payload["category"] == "config"
    assert payload["kind"] == "interrupted"
    assert "unclassified provider error" not in payload["message"]


@pytest.mark.anyio
async def test_runner_outer_except_unknown_when_cancel_unset(
    tmp_path: Path,
) -> None:
    """Regression guard: with `_cancel` False the unknown-classifier path runs."""
    class _ExplodingClient:
        async def __aenter__(self) -> None:
            raise RuntimeError("SDK broke unexpectedly")
        async def __aexit__(self, *args: object) -> None:
            return None

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda _opts: _ExplodingClient(),
    )
    inv = _inv(tmp_path)
    events = [ev async for ev in runner.run(inv)]
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert error_events[0]["payload"]["category"] == "unknown"


@pytest.mark.anyio
async def test_receive_loop_sweeps_bg_per_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-API-site analog: the runner sweeps once per received SDK message,
    so a backgrounded flooder is cap-checked even on turns with no imp tool
    dispatch (the CC-side web/builtin turns)."""
    import imp.agents.tools.bg as bg_mod

    calls = {"n": 0}
    real = bg_mod._sweep_bg

    async def counting(ctx):
        calls["n"] += 1
        await real(ctx)

    monkeypatch.setattr(bg_mod, "_sweep_bg", counting)
    # Mirror test_runs_to_complete_step: flat scripted list, factory takes opts.
    # Adaptation from plan transcription: plan had nested list + lambda opts: fake;
    # real FakeClaudeSDKClient takes opts as first positional, scripted_messages kw.
    scripted = [
        assistant(text("thinking...")),
        assistant(tool_use("t1", "complete_step", {"status": "Success"})),
        result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=fake_client_factory)
    events = [e async for e in runner.run(_inv(tmp_path))]
    assert events[-1]["type"] == "agent_output"
    assert calls["n"] >= 2          # at least one sweep per received message


async def _run_events(tmp_path: Path, scripted: list, *, max_tool_calls: int,
                      log: InMemoryEventLog) -> list:
    inv0 = _inv(tmp_path, tools=frozenset({"complete_step", "task"}), event_log=log)
    from dataclasses import replace as _replace
    inv = _replace(inv0, max_tool_calls=max_tool_calls)

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory, user_input_fn=None)
    return [ev async for ev in runner.run(inv)]


class _CLIConnectionError(Exception):
    """Local stand-in for the SDK's CLIConnectionError. Matched by class name
    (`_is_terminated_process_teardown`) — but that helper requires the exact
    name "CLIConnectionError", so expose it under that __name__."""


_CLIConnectionError.__name__ = "CLIConnectionError"


class _StallingClient:
    """A claude CLI child that wedged mid-turn: receive_response() parks forever
    awaiting a model-API stream that never arrives. disconnect() kills the
    transport subprocess, which unblocks the parked read with a
    terminated-process error — exactly the SDK's behavior when its child dies."""

    def __init__(self, opts: Any) -> None:
        self.opts = opts
        self.disconnected = False
        import asyncio
        self._killed = asyncio.Event()

    async def __aenter__(self) -> _StallingClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        self.disconnected = True
        self._killed.set()
        return False

    async def query(self, _prompt: Any) -> None:
        pass

    async def receive_response(self) -> Any:
        await self._killed.wait()
        raise _CLIConnectionError("Cannot write to terminated process")
        yield  # unreachable, but makes this an async generator

    async def disconnect(self) -> None:
        self.disconnected = True
        self._killed.set()

    async def interrupt(self) -> None:
        pass


@pytest.mark.anyio
async def test_interrupt_tears_down_stalled_sdk_client(tmp_path: Path) -> None:
    """Regression: the inactivity watchdog tears a stalled agent down via
    runner.interrupt(). For claude_code that MUST kill the live CLI subprocess —
    flipping _cancel alone never unblocks a receive_response() parked on a
    message that isn't coming, so the child would otherwise leak and race the
    watchdog's restart of the same step."""
    import asyncio

    (tmp_path / "art").mkdir()
    holder: dict[str, _StallingClient] = {}

    def factory(opts: Any) -> _StallingClient:
        c = _StallingClient(opts)
        holder["c"] = c
        return c

    inv = _inv(tmp_path)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events: list[dict[str, Any]] = []

    async def drive() -> None:
        async for ev in runner.run(inv):
            events.append(ev)

    task = asyncio.create_task(drive())
    # Let run() build the client and park in receive_response().
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    assert not task.done(), "agent should be stalled, not finished"

    await runner.interrupt()

    # interrupt() must break the parked stream; without the live-client
    # teardown the task hangs here forever (the leaked-subprocess bug).
    await asyncio.wait_for(task, timeout=2)

    assert holder["c"].disconnected is True
    assert events and events[-1]["type"] == "error"
    assert events[-1]["payload"]["kind"] == "interrupted"


@pytest.mark.anyio
async def test_interrupt_without_live_client_is_safe(tmp_path: Path) -> None:
    """interrupt() before run() (no client connected) must not raise."""
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    await runner.interrupt()
    await runner.interrupt()  # idempotent
    assert runner._cancel is True




@pytest.mark.anyio
async def test_result_is_error_emits_terminated_not_nudge(tmp_path: Path) -> None:
    """A ResultMessage flagged is_error is a termination: emit a typed `terminated`
    error with the structured fields in the detail, BEFORE any nudge — even when a
    continuation_fn is installed."""
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(text("Something went wrong and the agent stopped.")),
        result(is_error=True, subtype="error_during_execution", stop_reason="error"),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    inv = _inv(tmp_path)
    inv = replace(inv, continuation_fn=lambda _last: "please call complete_step")
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-6", client_factory=factory)

    events = [e async for e in runner.run(inv)]
    errs = [e for e in events if e.get("type") == "error"]
    assert len(errs) == 1
    payload = errs[0]["payload"]
    assert payload["category"] == "terminated"
    assert payload["detail"]["is_error"] is True
    assert payload["detail"]["subtype"] == "error_during_execution"
    assert payload["detail"]["stop_reason"] == "error"
    # Never routed through the benign nudge path.
    assert payload["kind"] != "no_complete_step_after_nudges"


@pytest.mark.anyio
async def test_result_is_error_reports_quota_wording_independent(tmp_path: Path) -> None:
    """A spend-limit termination is reported regardless of wording: 'monthly spend
    limit' is in no pattern list, so it is the generic `terminated` type, with the
    wording captured in the detail."""
    (tmp_path / "art").mkdir()
    wording = "You've hit your monthly spend limit · raise it at claude.ai/settings/usage"
    scripted = [assistant(text(wording)), result(is_error=True)]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-6", client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path))]
    errs = [e for e in events if e.get("type") == "error"]
    assert len(errs) == 1
    assert errs[0]["payload"]["category"] == "terminated"
    assert wording in errs[0]["payload"]["detail"]["last_agent_text"]


@pytest.mark.anyio
async def test_result_is_error_with_known_quota_text_labels_quota(tmp_path: Path) -> None:
    """A recognized usage-limit wording is labeled QuotaError (category quota), and
    still carries the structured fields in the detail."""
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(text("You have reached your usage limit for today.")),
        result(is_error=True),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-6", client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path))]
    errs = [e for e in events if e.get("type") == "error"]
    assert len(errs) == 1
    assert errs[0]["payload"]["category"] == "quota"
    assert errs[0]["payload"]["detail"]["is_error"] is True


@pytest.mark.anyio
async def test_clean_no_complete_step_still_nudges(tmp_path: Path) -> None:
    """is_error=False keeps today's benign behavior: a continuation_fn that gives
    up routes to agent_incomplete / no_complete_step_after_nudges, NOT terminated."""
    (tmp_path / "art").mkdir()
    scripted = [assistant(text("I'm still thinking about it.")), result()]   # is_error False

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    inv = _inv(tmp_path)
    inv = replace(inv, continuation_fn=lambda _last: None)
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-6", client_factory=factory)

    events = [e async for e in runner.run(inv)]
    errs = [e for e in events if e.get("type") == "error"]
    assert len(errs) == 1
    payload = errs[0]["payload"]
    assert payload["category"] == "agent_incomplete"
    assert payload["kind"] == "no_complete_step_after_nudges"


@pytest.mark.anyio
async def test_resume_bare_assistant_injects_continue_and_completes(tmp_path: Path) -> None:
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import RESUME_CONTINUE_NUDGE, Transcript
    inv = _inv(tmp_path)
    # MISSING_COMPLETE_STEP shape on disk at the step's transcript dir.
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="begin"),)))
    t.append(Message(role="assistant",
                     content=(TextBlock(text="You've hit your monthly spend limit"),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})), result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    inv = replace(inv, balance_on_resume=True)
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-6", client_factory=factory)
    events = [e async for e in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"
    assert not any(e.get("type") == "error" for e in events)
    msgs = t.load()
    assert any(isinstance(b, TextBlock) and b.text == RESUME_CONTINUE_NUDGE
               for m in msgs for b in m.content)


def test_termination_from_result_nonzero_exit_is_terminated() -> None:
    """Nonzero subprocess exit triggers TerminatedError independent of is_error."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    benign = "The build finished but complete_step was never called."
    term = cc._termination_from_result(result(is_error=False), benign, {"exit": 1})
    assert isinstance(term, perr.TerminatedError)
    assert term.detail["exit_code"] == 1
    assert term.detail["is_error"] is False
    # Falsifiability: a clean exit with is_error False is benign (no termination),
    # so the result above is driven by the nonzero-exit branch, not by is_error.
    assert cc._termination_from_result(result(is_error=False), benign, {"exit": 0}) is None


def test_termination_preserves_text_5xx_as_network() -> None:
    """An is_error ResultMessage whose last text is `API Error: 500 …` keeps the
    NetworkError category (network → RetryWithBackoff), not TerminatedError."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True), "API Error: 500 server error", {})
    assert isinstance(term, perr.NetworkError)
    assert term.category == "network"
    # Falsifiability: structured detail still flows through onto the preserved label.
    assert term.detail["is_error"] is True


def test_termination_preserves_text_429_as_rate_limit() -> None:
    """`API Error: 429 …` is the only 429 path; it keeps the RateLimitError category."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True), "API Error: 429 rate limited", {})
    assert isinstance(term, perr.RateLimitError)
    assert term.category == "rate_limit"


def test_termination_quota_and_auth_still_pause() -> None:
    """Terminal causes keep their pausing categories, unchanged."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    quota = cc._termination_from_result(
        result(is_error=True), "You have reached your usage limit", {})
    assert isinstance(quota, perr.QuotaError)
    auth = cc._termination_from_result(
        result(is_error=True), "Invalid API key", {})
    assert isinstance(auth, perr.AuthError)


def test_termination_unrecognized_text_is_terminated() -> None:
    """is_error with unrecognized text and null status stays TerminatedError (pauses)."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True), "I finished editing the file.", {})
    assert isinstance(term, perr.TerminatedError)


def test_termination_4xx_non_429_is_terminated() -> None:
    """A 4xx (non-429) api_error_status collapses to TerminatedError — ApiError
    is intentionally not in the preserved-type set, so 4xx stays as a pause."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True), "API Error: 400 bad request", {})
    assert isinstance(term, perr.TerminatedError)


def test_termination_api_error_status_5xx_is_network() -> None:
    """A 5xx api_error_status with code-less text classifies as NetworkError."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=500), "the request failed", {})
    assert isinstance(term, perr.NetworkError)
    assert term.category == "network"
    assert term.detail["api_error_status"] == 500


def test_termination_api_error_status_429_is_not_rate_limit() -> None:
    """There is no api_error_status=429 branch: a 429 status with code-less text
    stays TerminatedError (the only 429 retry path is the text classifier)."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=429), "the request failed", {})
    assert isinstance(term, perr.TerminatedError)


def test_termination_text_label_wins_over_api_error_status() -> None:
    """Text classification wins over api_error_status: if _classify_agent_text
    returns a label, the 5xx status fallback does not override it."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=500), "API Error: 429 rate limited", {})
    assert isinstance(term, perr.RateLimitError)
    assert term.category == "rate_limit"


def test_termination_codeless_phrase_null_status_is_network() -> None:
    """A code-less `API Error: Internal server error` with null status survives
    as NetworkError through _termination_from_result."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True), "API Error: Internal server error", {})
    assert isinstance(term, perr.NetworkError)
    assert term.category == "network"


def test_build_client_installs_session_isolation_on_real_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_client installs isolation on the real arm but not the factory arm."""
    import claude_agent_sdk as sdk

    import imp.providers.claude_code_session as sessmod

    installed: list[bool] = []
    monkeypatch.setattr(
        sessmod, "install_claude_session_isolation",
        lambda: installed.append(True))

    class _FakeSDKClient:
        def __init__(self, opts: Any) -> None:
            self.opts = opts

    monkeypatch.setattr(sdk, "ClaudeSDKClient", _FakeSDKClient)

    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5", client_factory=None)
    client = runner._build_client("OPTS")
    assert installed == [True], "isolation must be installed on the real-construction arm"
    assert isinstance(client, _FakeSDKClient)
    assert client.opts == "OPTS"

    installed.clear()
    factory_runner = ClaudeCodeAgentRunner(
        "claude-sonnet-4-5", client_factory=lambda opts: ("factory", opts))
    result = factory_runner._build_client("OPTS2")
    assert result == ("factory", "OPTS2")
    assert installed == [], "the test-factory arm must not install isolation"


@pytest.mark.anyio
async def test_task_tool_call_carries_subagent_label(tmp_path, monkeypatch) -> None:
    from imp.agents.tools.context import ToolContext
    from imp.providers import claude_code as cc

    async def _fake_dispatch(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return "sub done"

    async def _noop_sweep(ctx):
        return None

    monkeypatch.setattr(cc, "dispatch_task", _fake_dispatch)
    monkeypatch.setattr(cc._bg, "_sweep_bg", _noop_sweep)

    log = InMemoryEventLog()
    explorer = replace(_agent_def(), name="explorer", label="exploring codebase")
    ctx = ToolContext(
        project_root=tmp_path,events_path=tmp_path / "e.jsonl",
        subagent_defs={"explorer": explorer}, event_log=log)
    state = cc._RunState(agent_id="step:p/s")

    class _StubTool:
        description = ""
        input_schema: ClassVar[dict] = {}

    def _fake_sdk_tool(name, desc, schema, annotations=None):
        def deco(fn):
            return fn
        return deco

    impl = cc._wrap_one("task", _StubTool(), ctx, state, _fake_sdk_tool)
    await impl({"subagent_name": "explorer", "task_description": "go"})

    call = next(e for e in log.read() if e["type"] == "tool_call")
    assert call["payload"]["name"] == "task"
    assert call["payload"]["label"] == "exploring codebase"


@pytest.mark.anyio
async def test_task_tool_call_omits_label_when_absent(tmp_path, monkeypatch) -> None:
    from imp.agents.tools.context import ToolContext
    from imp.providers import claude_code as cc

    async def _fake_dispatch(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return "sub done"

    async def _noop_sweep(ctx):
        return None

    monkeypatch.setattr(cc, "dispatch_task", _fake_dispatch)
    monkeypatch.setattr(cc._bg, "_sweep_bg", _noop_sweep)

    log = InMemoryEventLog()
    explorer = replace(_agent_def(), name="explorer", label=None)
    ctx = ToolContext(
        project_root=tmp_path,events_path=tmp_path / "e.jsonl",
        subagent_defs={"explorer": explorer}, event_log=log)
    state = cc._RunState(agent_id="step:p/s")

    class _StubTool:
        description = ""
        input_schema: ClassVar[dict] = {}

    def _fake_sdk_tool(name, desc, schema, annotations=None):
        def deco(fn):
            return fn
        return deco

    impl = cc._wrap_one("task", _StubTool(), ctx, state, _fake_sdk_tool)
    # Case 1: subagent name not in subagent_defs
    await impl({"subagent_name": "ghost", "task_description": "haunt"})
    # Case 2: subagent exists but label=None
    await impl({"subagent_name": "explorer", "task_description": "go"})

    calls = [e for e in log.read() if e["type"] == "tool_call"]
    assert len(calls) == 2
    assert "label" not in calls[0]["payload"]
    assert "label" not in calls[1]["payload"]


def test_build_options_carries_session_store_and_id(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    store = session_store_for(tmp_path)
    opts = runner._build_options(
        _inv(tmp_path), object(), ["complete_step"],
        session_store=store, session_id="abc",
    )
    assert opts.session_store is store
    assert opts.session_id == "abc"


def test_build_options_defaults_to_no_session_store(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    opts = runner._build_options(_inv(tmp_path), object(), ["complete_step"])
    assert opts.session_store is None
    assert opts.session_id is None


@pytest.mark.anyio
async def test_fresh_run_mirrors_session_into_store(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    inv = _inv(tmp_path, agent_id="step:p/s")
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert isinstance(opts.session_store, ImpSessionStore)
    assert opts.session_id == resume_session_id("step:p/s")


@pytest.mark.anyio
async def test_resume_without_stored_session_passes_no_session_state(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="thinking…"),
            ToolUseBlock(id="t0", name="read_file", input={"path": "/p/x"}),
        ),
    ))
    t.append(Message(
        role="user", content=(ToolResultBlock(tool_use_id="t0", content="x"),),
    ))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.session_store is None
    assert opts.session_id is None
    assert opts.resume is None


def _user_text_blob(replayed: list[Any]) -> str:
    # Consecutive user turns merge before stream-json replay, so collect text from
    # both plain-string and block-list user turns.
    out: list[str] = []
    for d in replayed:
        if d.get("type") != "user":
            continue
        content = d["message"]["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(b["text"] for b in content if b.get("type") == "text")
    return "\n".join(out)


@pytest.mark.anyio
async def test_resume_injects_succeeded_manifest_into_replay(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="reading…"),
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),
    )))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=True),)))
    # Ends on assistant prose, no complete_step → MISSING_COMPLETE_STEP (ckx9f5).
    t.append(Message(role="assistant", content=(TextBlock(text="done early"),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    [ev async for ev in runner.run(inv)]

    blob = _user_text_blob(created[0].queries[0])
    assert "read_file — succeeded" in blob
    assert "replay artifact" in blob


@pytest.mark.anyio
async def test_resume_balance_path_keeps_retry_feedback_last(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    # A previously-completed step re-invoked with reviewer/gate retry feedback.
    inv = replace(
        inv, balance_on_resume=True, retry_feedback="REVISE: rewrite plan.md")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    # A durable write the model must now REVISE, not preserve.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="w1", name="mcp__imp__write_file",
                     input={"path": "plan.md", "content": "OLDBODY"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="w1", content="ok", is_error=True),)))
    # Ends on a SUCCESSFUL complete_step (no result) → the balance path.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="c0", name="complete_step",
                     input={"status": "Success"}),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    [ev async for ev in runner.run(inv)]

    blob = _user_text_blob(created[0].queries[0])
    # The retry feedback IS present and the "do not redo" manifest is NOT —
    # so nothing displaces the revision instruction.
    assert "REVISE: rewrite plan.md" in blob
    assert "replay artifact" not in blob
    assert "write_file — succeeded" not in blob
    # The retry feedback is the LAST text the model reads on resume.
    assert blob.rstrip().endswith("REVISE: rewrite plan.md")


@pytest.mark.anyio
async def test_resume_manifest_skips_dangling_last_tool_use(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    # A completed write — must be named succeeded with its echoed body.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="w1", name="mcp__imp__write_file",
                     input={"path": "plan.md", "content": "PLANBODY"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="w1", content="ok", is_error=True),)))
    # A genuinely-dangling read — killed between call and result, no tool_result.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="r2", name="mcp__imp__read_file",
                     input={"path": "x.md"}),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    [ev async for ev in runner.run(inv)]

    blob = _user_text_blob(created[0].queries[0])
    # The completed write is named succeeded with its echoed deliverable…
    assert "write_file — succeeded (wrote plan.md)" in blob
    assert "PLANBODY" in blob
    # …but the genuinely-dangling read is NOT claimed succeeded.
    assert "read_file — succeeded" not in blob
    # The dangling tool_use is still repaired: a tool_result for r2 was persisted.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert any(
        isinstance(b, ToolResultBlock) and b.tool_use_id == "r2"
        for m in loaded
        for b in m.content
    )


@pytest.mark.anyio
async def test_resume_manifest_not_re_injected_on_re_resume(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="reading…"),
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),
    )))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=True),)))
    t.append(Message(role="assistant", content=(TextBlock(text="done early"),)))
    # Simulate a prior resume that already injected the nudge + manifest.
    t.append(Message(role="user", content=(TextBlock(
        text="You stopped before calling complete_step. Continue exactly where you "
             "left off and finish the step — call complete_step when the work is done."),)))
    t.append(Message(role="user", content=(TextBlock(
        text=RESUME_MANIFEST_HEADER + "\n\n- read_file — succeeded"),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    [ev async for ev in runner.run(inv)]

    blob = _user_text_blob(created[0].queries[0])
    # The prior manifest is present (merged into the replay query)…
    assert "replay artifact" in blob
    # …but NOT re-injected a second time.
    assert blob.count("replay artifact") == 1


def test_build_options_carries_resume(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    opts = runner._build_options(
        _inv(tmp_path), object(), ["complete_step"], resume="rid-123")
    assert opts.resume == "rid-123"


def test_build_options_defaults_to_no_resume(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    opts = runner._build_options(_inv(tmp_path), object(), ["complete_step"])
    assert opts.resume is None


@pytest.mark.skip(
    reason="TEMP (change wxczhw): native resume is forced off in claude_code.py "
    "until the CLAUDE_CONFIG_DIR-isolation fix lands; this asserts the disabled "
    "native-resume routing. Un-skip alongside removing the `native_resume = False` "
    "override.")
@pytest.mark.anyio
async def test_resume_with_stored_session_routes_native(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import (
        RESUME_CONTINUE_NUDGE,
        RESUME_MANIFEST_HEADER,
        Transcript,
    )
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    # A prior fresh run mirrored its session into the store under this step's key.
    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="reading…"),
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),
    )))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=True),)))
    # Ends on assistant prose, no complete_step → MISSING_COMPLETE_STEP, whose
    # inject_continue_nudge_on_resume APPENDS RESUME_CONTINUE_NUDGE (one new turn).
    # The loaded session ends on this assistant prose, so native resume is valid.
    t.append(Message(role="assistant", content=(TextBlock(text="done early"),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")
    assert isinstance(opts.session_store, ImpSessionStore)
    assert opts.session_id is None
    # Native resume sends ONLY the post-resume nudge turn, not replayed history.
    first_query = created[0].queries[0]
    assert len(first_query) == 1
    blob = _user_text_blob(first_query)
    assert RESUME_CONTINUE_NUDGE in blob
    assert "reading" not in blob
    # The faithful native path injects no fallback manifest.
    assert RESUME_MANIFEST_HEADER not in blob


@pytest.mark.anyio
async def test_resume_without_stored_session_falls_back(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    # No session store pre-populated → native resume is unavailable.

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="reading…"),
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),
    )))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=True),)))
    t.append(Message(role="assistant", content=(TextBlock(text="done early"),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume is None
    assert opts.session_store is None
    # Fallback streams the WHOLE history (more than one stream-json frame)…
    first_query = created[0].queries[0]
    assert len(first_query) > 1
    # …and injects the Part-3 manifest naming prior calls succeeded.
    blob = _user_text_blob(first_query)
    assert RESUME_MANIFEST_HEADER in blob
    assert "read_file — succeeded" in blob


@pytest.mark.anyio
async def test_native_resume_downgrades_when_paused_on_tool_result(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    # Ends on a COMPLETE user tool_result (SEND_TO_LLM): the loaded session ends
    # on a user turn, so native resume would have to append a second consecutive
    # user turn (or an empty slice). Must downgrade to the stream-json fallback.
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume is None
    assert opts.session_store is None
    first_query = created[0].queries[0]
    assert len(first_query) > 1
    assert RESUME_MANIFEST_HEADER in _user_text_blob(first_query)


@pytest.mark.anyio
async def test_native_resume_downgrades_on_inplace_repair(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import (
        RESUME_DURABILITY_NOTE,
        RESUME_MANIFEST_HEADER,
        Transcript,
    )
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    # Completed read round-trip → manifest names it succeeded.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "a.txt"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)))
    # Dangling read whose interrupt marker persisted as the trailing user turn:
    # t1 has no tool_result → shape-2 in-place repair, loaded session ends on user.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="mcp__imp__read_file",
                     input={"path": "b.txt"}),)))
    t.append(Message(role="user", content=(
        TextBlock(text="[Request interrupted by user]"),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Loaded session ends on a user turn → downgraded to fallback.
    assert opts.resume is None
    assert opts.session_store is None
    first_query = created[0].queries[0]
    assert len(first_query) > 1
    blob = _user_text_blob(first_query)
    # Fallback manifest names the COMPLETED read (t0); the dangling read (t1) is
    # excluded, so only one "succeeded" line.
    assert RESUME_MANIFEST_HEADER in blob
    assert blob.count("read_file — succeeded") == 1
    # The in-place-rewritten user turn (carrying t1's synthesized result) is
    # streamed in position — its durability note rides on that same turn.
    assert RESUME_DURABILITY_NOTE in blob


@pytest.mark.anyio
async def test_native_resume_downgrades_on_dangling_tool_use(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    # Completed read round-trip → the fallback manifest names it succeeded.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "a.txt"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)))
    # Mid-tool interrupt: a bare assistant tool_use with NO tool_result. The SDK
    # batcher never flushed this turn (no `result`), so claude-p's loaded session
    # ends before it and resume_base excludes it from the slice — native resume
    # would orphan the repair's synthesized tool_result. classify_resume(existing)
    # is DANGLING_TOOL_USE (not SEND_TO_LLM, not MISSING_COMPLETE_STEP).
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="mcp__imp__read_file",
                     input={"path": "b.txt"}),)))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Downgraded to the stream-json fallback despite a populated store.
    assert opts.resume is None
    assert opts.session_store is None
    first_query = created[0].queries[0]
    assert len(first_query) > 1
    blob = _user_text_blob(first_query)
    # Fallback manifest names the COMPLETED read (t0); the dangling read (t1) is
    # excluded, so exactly one "succeeded" line.
    assert RESUME_MANIFEST_HEADER in blob
    assert blob.count("read_file — succeeded") == 1


@pytest.mark.anyio
async def test_native_resume_downgrades_on_awaiting_user(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    # Completed tool round-trip.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "a.txt"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)))
    # AWAITING_USER final turn: unanswered wait_for_user tool_use with no
    # tool_result — the SDK batcher never flushed this turn.
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="need a decision"),
            ToolUseBlock(id="wfu1", name="wait_for_user",
                         input={"prompt": "merge?"}),
        ),
    ))

    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []
    user_inputs = iter(["yes, proceed"])

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory,
        user_input_fn=lambda _prompt: next(user_inputs),
    )
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Downgraded to the stream-json fallback despite a populated store.
    assert opts.resume is None
    assert opts.session_store is None
