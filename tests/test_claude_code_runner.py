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
    session_store_for,
)
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    result,
    text,
    thinking,
    tool_result,
    tool_use,
    user,
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
    # Initial messages were NOT re-appended: no-store SEND_TO_LLM now
    # resumes natively — no manifest turn, so 3 original + 1 new assistant = 4.
    loaded = t.load()
    assert len(loaded) == 4


@pytest.mark.anyio
async def test_runs_subagent_via_task(tmp_path: Path) -> None:
    """Verifies the parent's receive-loop dual-write for complete_step and the
    resulting agent_output payload.

    The fake bypasses the MCP transport, so _impl for sub_agent is never
    invoked and no child runner is created — the child text-deliverable path is
    integration-only. What is exercised: the dual-write at the AssistantMessage
    tool_use scan (complete_step → state.complete_step_args) and the agent_output
    event that follows.
    """
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

    parent_def = _agent_def(tools=frozenset({"complete_step", "sub_agent"}))
    inv = _inv(tmp_path)
    inv = replace(
        inv,
        agent_def=parent_def,
        subagent_defs={"sub": sub_def},
    )

    parent_scripted = [
        assistant(
            text("dispatching"),
            tool_use("p1", "sub_agent",
                     {"subagent_name": "sub", "task_description": "do work"}),
        ),
        assistant(
            text("got result"),
            tool_use("p2", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=parent_scripted)

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

    parent_def = _agent_def(tools=frozenset({"complete_step", "sub_agent"}))
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    inv = replace(inv, agent_def=parent_def)

    # Simulate _impl having already logged the canonical task call+result on the
    # MINTED block id (distinct from the SDK's toolu_… id-space).
    minted_id = "minted-deadbeef"
    sdk_id = "toolu_sdk_echo"
    log.append_tool_call(
        agent_id="step:p/s",
        block=RealToolUseBlock(id=minted_id, name="sub_agent",
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
            tool_use(sdk_id, "sub_agent",
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
        t for rec in observed for t in rec.tools if t.name == "sub_agent"
    ]
    assert task_outcomes, "no task ToolOutcome reached the LoopGuard"
    assert all(t.is_error is False for t in task_outcomes), (
        "task is_error not flipped to the echoed False (stuck at default True)")


@pytest.mark.anyio
async def test_bare_turn_end_resume_delivers_reply_then_continues(
    tmp_path: Path,
) -> None:
    """AWAITING_USER resume (claude_code): a paused transcript ends on a bare
    assistant turn; the runner solicits the human and delivers the reply as a
    plain user TextBlock message (NOT a tool_result) before driving the next LLM
    call."""
    from imp.agents.runner import Message, TextBlock, ToolResultBlock
    from imp.agents.transcript import Transcript
    from tests.cc_fakes import (
        FakeClaudeSDKClient,
        assistant,
        result,
        text,
        tool_use,
    )

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="what color?"),)))

    scripted = [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    user_inputs = iter(["blue"])

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

    kinds = [e["type"] for e in events]
    assert kinds[-1] == "agent_output"

    log_types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" in log_types
    assert log_types.index("user_message") < log_types.index("llm_response")

    loaded = t.load()
    assert loaded[2].role == "user"
    block = loaded[2].content[0]
    assert isinstance(block, TextBlock)
    assert not isinstance(block, ToolResultBlock)
    assert block.text == "blue"


@pytest.mark.anyio
async def test_bare_turn_end_resume_no_channel_errors(tmp_path: Path) -> None:
    """A headless resume (no user_input_fn) of a bare-turn-end AWAITING_USER
    transcript yields a clean no_user_input_fn error and never queries the SDK.
    (The inert-resume CLI that idles for a human instead is a later change.)"""
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="what color?"),)))

    queried = {"hit": False}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        queried["hit"] = True
        return FakeClaudeSDKClient(opts, scripted_messages=[result()])

    runner = ClaudeCodeAgentRunner(  # no user_input_fn -> no reply channel
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(inv)]
    kinds = [e["type"] for e in events]

    assert "agent_output" not in kinds
    assert queried["hit"] is False, "no transcript may be streamed to the provider"
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["payload"]["kind"] == "no_user_input_fn"


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
                file=Path("agents/a.md"), tier="low")
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
    inv0 = _inv(tmp_path, tools=frozenset({"complete_step", "sub_agent"}), event_log=log)
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
    """Escalation tier: a genuinely frozen claude-p does not answer the
    cooperative client.interrupt(), so interrupt() must escalate to the hard
    kill — disconnect() kills the live CLI subprocess and unblocks the parked
    receive_response(), else the child leaks and races the watchdog's restart."""
    import asyncio

    (tmp_path / "art").mkdir()
    holder: dict[str, _StallingClient] = {}

    def factory(opts: Any) -> _StallingClient:
        c = _StallingClient(opts)
        holder["c"] = c
        return c

    inv = _inv(tmp_path)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        steer_timeout=0.05)

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
async def test_force_kill_sigkills_transport_group_without_waiting(
    tmp_path: Path, monkeypatch,
) -> None:
    """force_kill() SIGKILLs the live transport process group and never awaits the child."""
    import asyncio
    import signal

    import imp.providers.claude_code as cc

    calls: list[tuple[int | None, int]] = []

    async def _record(process: Any, sig: int) -> None:
        calls.append((getattr(process, "pid", None), sig))

    monkeypatch.setattr(cc, "kill_process_group", _record)

    class _Proc:
        pid = 4242

        def __init__(self) -> None:
            self.waited = False

        async def wait(self) -> int:
            self.waited = True
            return 0

    class _Transport:
        def __init__(self, proc: _Proc) -> None:
            self._process = proc

    (tmp_path / "art").mkdir()
    holder: dict[str, _StallingClient] = {}

    def factory(opts: Any) -> _StallingClient:
        c = _StallingClient(opts)
        # Attach a live transport+process so force_kill has a group to SIGKILL.
        c._transport = _Transport(_Proc())
        holder["c"] = c
        return c

    inv = _inv(tmp_path)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory, steer_timeout=0.05)

    async def drive() -> None:
        async for _ev in runner.run(inv):
            pass

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await runner.force_kill()

    proc = holder["c"]._transport._process
    assert calls == [(4242, signal.SIGKILL)]
    assert proc.waited is False   # fire-and-forget: never reaps the child

    # Clean up the parked run() task via the cooperative interrupt path.
    await runner.interrupt()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.anyio
async def test_force_kill_without_live_client_is_safe() -> None:
    """force_kill() before run() (no client/transport) must not raise."""
    import imp.providers.claude_code as cc

    runner = cc.ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    await runner.force_kill()   # no client connected; must be a safe no-op


class _NoCoopClient:
    """A live client that ignores cooperative interrupt(): receive_response()
    parks forever, so interrupt() must escalate to disconnect()."""

    def __init__(self, opts: Any, *, order: list[str]) -> None:
        self.opts = opts
        self.order = order
        self.disconnected = False
        import asyncio
        self._killed = asyncio.Event()

    async def __aenter__(self) -> _NoCoopClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        self.disconnected = True
        self._killed.set()
        return False

    async def query(self, _prompt: Any) -> None:
        pass

    async def receive_response(self) -> Any:
        await self._killed.wait()
        return
        yield  # makes this an async generator

    async def interrupt(self) -> None:
        self.order.append("interrupt")  # cooperative tier tried first

    async def disconnect(self) -> None:
        self.order.append("disconnect")  # escalation tier
        self.disconnected = True
        self._killed.set()


@pytest.mark.anyio
async def test_interrupt_tries_cooperative_then_escalates_on_timeout(
    tmp_path: Path,
) -> None:
    import asyncio

    (tmp_path / "art").mkdir()
    order: list[str] = []
    holder: dict[str, _NoCoopClient] = {}

    def factory(opts: Any) -> _NoCoopClient:
        c = _NoCoopClient(opts, order=order)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        steer_timeout=0.05)

    events: list[dict[str, Any]] = []

    async def drive() -> None:
        async for ev in runner.run(_inv(tmp_path)):
            events.append(ev)

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await runner.interrupt()
    await asyncio.wait_for(task, timeout=2)

    # Cooperative interrupt() fired FIRST, then escalated to disconnect().
    assert order == ["interrupt", "disconnect"], order
    assert holder["c"].disconnected is True
    assert events and events[-1]["payload"]["kind"] == "interrupted"


@pytest.mark.anyio
async def test_repeat_interrupt_escalates_despite_prior_steer(
    tmp_path: Path,
) -> None:
    """Regression for the stale-set unblock latch: a prior cooperative steer
    leaves _steer_unblocked set, so a SECOND interrupt on a now-frozen agent
    must STILL escalate. Without the per-interrupt clear() the wait_for returns
    immediately on the stale-set Event and the hard kill is silently skipped."""
    import asyncio

    (tmp_path / "art").mkdir()
    order: list[str] = []
    holder: dict[str, _NoCoopClient] = {}

    def factory(opts: Any) -> _NoCoopClient:
        c = _NoCoopClient(opts, order=order)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        steer_timeout=0.05)

    events: list[dict[str, Any]] = []

    async def drive() -> None:
        async for ev in runner.run(_inv(tmp_path)):
            events.append(ev)

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    # Simulate the stale-set latch left behind by a prior cooperative steer.
    runner._steer_unblocked.set()

    await runner.interrupt()
    await asyncio.wait_for(task, timeout=2)

    # The clear() forces a fresh wait that times out and escalates.
    assert order == ["interrupt", "disconnect"], order
    assert holder["c"].disconnected is True


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


def test_termination_api_error_status_429_is_rate_limit() -> None:
    """A 429 api_error_status classifies as RateLimitError (recoverable backoff),
    not TerminatedError, even when the agent text carries no routable code."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=429), "the request failed", {})
    assert isinstance(term, perr.RateLimitError)
    assert not isinstance(term, perr.TerminatedError)


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


def test_termination_api_error_status_401_is_auth() -> None:
    """A 401 status with code-less text classifies as AuthError (not Terminated),
    carrying kind=http_401 so the eventual pause reads as a credential issue."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=401), "the request failed", {})
    assert isinstance(term, perr.AuthError)
    assert term.category == "auth"
    assert getattr(term, "kind", "") == "http_401"
    assert term.detail["api_error_status"] == 401


def test_termination_api_error_status_403_is_auth() -> None:
    """A 403 status with code-less text classifies as AuthError (not Terminated),
    carrying kind=http_403."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=403), "the request failed", {})
    assert isinstance(term, perr.AuthError)
    assert term.category == "auth"
    assert getattr(term, "kind", "") == "http_403"
    assert term.detail["api_error_status"] == 403


def test_termination_api_error_status_400_stays_terminated() -> None:
    """A 4xx-bug status (400) with code-less text is NOT rescued by the auth
    fallback — it stays TerminatedError (immediate pause), as before."""
    from imp.providers import claude_code as cc
    from imp.providers import errors as perr

    term = cc._termination_from_result(
        result(is_error=True, api_error_status=400), "the request failed", {})
    assert isinstance(term, perr.TerminatedError)


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

    impl = cc._wrap_one("sub_agent", _StubTool(), ctx, state, _fake_sdk_tool)
    await impl({"subagent_name": "explorer", "task_description": "go"})

    call = next(e for e in log.read() if e["type"] == "tool_call")
    assert call["payload"]["name"] == "sub_agent"
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

    impl = cc._wrap_one("sub_agent", _StubTool(), ctx, state, _fake_sdk_tool)
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
async def test_resume_without_stored_session_passes_native_session_state(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)
    # No session store → reconstructs from the transcript and goes native.

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="thinking…"),
        ToolUseBlock(id="t0", name="read_file", input={"path": "/p/x"}),
    )))
    # Ends on a user tool_result turn → classify_resume → SEND_TO_LLM, a
    # native-eligible tail with no needs_imp_balance veto.
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="x"),)))

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
    # Native session state IS now passed (inverse of the old no-store contract):
    # the store is reconstructed and the resume id is set; session_id stays None.
    assert isinstance(opts.session_store, ImpSessionStore)
    assert opts.session_id is None
    assert opts.resume == resume_session_id("step:p/s")
    # SEND_TO_LLM folds the trailing user tool_result OUT of resume_base and
    # streams it as the delta, so 2 flushed turns are reconstructed (not 3).
    reconciled = await store.load(skey)
    assert len(reconciled) == 2


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


def _seed_complete_step_resume(
    tmp_path: Path, *, retry_feedback: str | None,
) -> AgentInvocation:
    """A previously-completed step: transcript ends on a SUCCESSFUL complete_step
    tool_use (no result). balance_on_resume=True mirrors every kept-transcript
    resume; retry_feedback carries the optional gate/review handback."""
    from imp.agents.runner import ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True, retry_feedback=retry_feedback)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="c0", name="complete_step",
                     input={"status": "Success"}),)))
    return inv


@pytest.mark.anyio
async def test_complete_step_tail_with_feedback_native_resumes(tmp_path: Path) -> None:
    """The complete_step-tail + retry_feedback shape no longer forces replay: it
    takes the native path (opts.resume set, session_id unset)."""
    inv = _seed_complete_step_resume(tmp_path, retry_feedback="REVISE: rewrite plan.md")
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    assert created[0].opts.resume == resume_session_id("step:p/s")
    assert created[0].opts.session_id is None


@pytest.mark.anyio
async def test_complete_step_tail_balanced_record_before_load(tmp_path: Path) -> None:
    """Before claude-p loads the native session, the stored record is balanced:
    the complete_step tool_use carries a synthesized non-error success result and
    there is no dangling tool_use."""
    from imp.providers.claude_code_session_store import session_key

    inv = _seed_complete_step_resume(tmp_path, retry_feedback="REVISE: rewrite plan.md")
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    entries = await store.load(skey)
    # The reconciled record ends on the synthesized success-result USER turn —
    # the complete_step tool_use is paired, not dangling.
    last = entries[-1]
    assert last["message"]["role"] == "user"
    results = [b for b in last["message"]["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert results and results[0]["tool_use_id"] == "c0"
    assert results[0]["is_error"] is False


@pytest.mark.anyio
async def test_complete_step_tail_handback_rides_streamed_delta(tmp_path: Path) -> None:
    """The handback rides the streamed delta (messages[resume_base:]) — the success
    result stays in the loaded session, never the delta."""
    inv = _seed_complete_step_resume(tmp_path, retry_feedback="REVISE: rewrite plan.md")
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    delta = created[0].queries[0]
    # Exactly one streamed user turn — the handback.
    assert _user_text_blob(delta).strip() == "REVISE: rewrite plan.md"
    # The success tool_result is NOT in the delta (it is in the loaded session).
    assert not any(
        d.get("type") == "user"
        and isinstance(d["message"]["content"], list)
        and any(b.get("type") == "tool_result" and b.get("tool_use_id") == "c0"
                for b in d["message"]["content"])
        for d in delta)


@pytest.mark.anyio
async def test_complete_step_tail_no_feedback_kicks_with_redo_nudge(tmp_path: Path) -> None:
    """A bare backward goto (no handback) keeps the success result in the loaded
    session and kicks the otherwise-empty delta with the redo nudge so claude-p
    generates instead of deadlocking on an empty stream."""
    from imp.agents.transcript import RESUME_REDO_NUDGE

    inv = _seed_complete_step_resume(tmp_path, retry_feedback=None)
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    assert created[0].opts.resume == resume_session_id("step:p/s")
    assert _user_text_blob(created[0].queries[0]).strip() == RESUME_REDO_NUDGE


@pytest.mark.anyio
async def test_no_store_awaiting_user_injects_no_manifest(tmp_path: Path) -> None:
    """A no-store AWAITING_USER tail now resumes natively, so the
    stream-json 'succeeded' replay manifest is never built — the streamed delta
    is just the human reply (the inverse of this test's pre-3a contract)."""
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
    # Bare turn-end → AWAITING_USER, native-eligible.
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
        user_input_fn=lambda _prompt: "keep going")
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")  # native path, not replay
    blob = _user_text_blob(created[0].queries[0])
    # No replay manifest is built on the native path…
    assert RESUME_MANIFEST_HEADER not in blob
    assert "replay artifact" not in blob
    assert "read_file — succeeded" not in blob
    # …only the human reply kicks the resumed turn.
    assert "keep going" in blob


@pytest.mark.anyio
async def test_native_resume_streams_retry_feedback_last(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    # A previously-completed step re-invoked with reviewer/gate retry feedback —
    # native resume, with the feedback delivered on the streamed delta turn.
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
    # The retry feedback IS present on the streamed delta and no replay manifest
    # is built (native path) — so nothing displaces the revision instruction.
    assert "REVISE: rewrite plan.md" in blob
    assert "replay artifact" not in blob
    assert "write_file — succeeded" not in blob
    # The retry feedback is the LAST text the model reads on resume.
    assert blob.rstrip().endswith("REVISE: rewrite plan.md")


@pytest.mark.anyio
async def test_no_store_dangling_tail_injects_no_manifest(tmp_path: Path) -> None:
    """A no-store DANGLING tail now resumes natively. No replay manifest
    is built (so the completed write is NOT named succeeded in a manifest); the
    dangling read is repaired by a STREAMED synth tool_result, and the flushed
    prefix is reconstructed into the native session."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
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
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    # Native resume reached completion — NOT the transcript_invalid abort.
    assert events[-1]["type"] == "agent_output"
    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None
    # No replay manifest anywhere in the persisted transcript.
    replayed = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert not any(
        isinstance(b, TextBlock) and b.text.startswith(RESUME_MANIFEST_HEADER)
        for m in replayed if m.role == "user" for b in m.content)
    # The dangling r2 read is repaired by a STREAMED synth tool_result.
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    assert streamed[0]["message"]["content"][0]["type"] == "tool_result"
    assert streamed[0]["message"]["content"][0]["tool_use_id"] == "r2"
    # The flushed prefix (write + dangling read) is reconstructed; r2 is the
    # trailing native-session entry, the synth result is streamed, not stored.
    reconciled = await store.load(skey)
    assert len(reconciled) == 4
    assert reconciled[-1]["message"]["content"][0] == {
        "type": "tool_use", "id": "r2",
        "name": "mcp__imp__read_file", "input": {"path": "x.md"}}


@pytest.mark.anyio
async def test_no_store_native_resume_does_not_reinject_manifest(tmp_path: Path) -> None:
    """A re-resume whose transcript already carries a prior manifest turn
    now resumes natively (no-store SEND_TO_LLM). The native path builds no new
    manifest, so the prior manifest text rides the streamed delta exactly once and
    is never stacked a second time. (The pre-3a replay-dedup guard at
    claude_code.py:1469-1476 is now reachable only via a non-production
    balance_on_resume=False config — see Decisions — so this is the
    production-faithful single-occurrence assertion.)"""
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
    # A prior resume already injected the nudge + manifest as trailing user turns.
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
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")  # native, not replay
    blob = _user_text_blob(created[0].queries[0])
    # The two trailing user turns (nudge + prior manifest) fold OUT of the
    # native session and ride the streamed SEND_TO_LLM delta…
    assert "replay artifact" in blob
    # …exactly once — the native path injects no second manifest.
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


@pytest.mark.anyio
async def test_resume_with_stored_session_routes_native(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import (
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
    # Ends on a bare turn-end → AWAITING_USER. The attended resume delivers the
    # user's reply as the next user message; native resume is valid (the store has
    # the session body flushed at the previous result boundary).
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
        user_input_fn=lambda _prompt: "keep going")
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")
    assert isinstance(opts.session_store, ImpSessionStore)
    assert opts.session_id is None
    # Native resume sends ONLY the post-resume reply turn, not replayed history.
    first_query = created[0].queries[0]
    assert len(first_query) == 1
    blob = _user_text_blob(first_query)
    assert "keep going" in blob
    assert "reading" not in blob
    # The faithful native path injects no fallback manifest.
    assert RESUME_MANIFEST_HEADER not in blob


@pytest.mark.anyio
async def test_resume_without_stored_session_reconstructs_native(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)
    # No session store pre-populated → reconstructs it from the transcript.

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="reading…"),
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),
    )))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=True),)))
    # Bare turn-end → AWAITING_USER, a native-eligible tail.
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
        user_input_fn=lambda _prompt: "keep going")
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None
    assert isinstance(opts.session_store, ImpSessionStore)
    # The no-store branch reconstructed and persisted the 4 pre-resume turns.
    reconciled = await store.load(skey)
    assert len(reconciled) == 4
    assert reconciled[-1]["message"]["content"][0]["text"] == "done early"
    # Native AWAITING_USER streams ONLY the human reply as the delta — no replay
    # of prior history, no fallback manifest.
    first_query = created[0].queries[0]
    assert len(first_query) == 1
    blob = _user_text_blob(first_query)
    assert "keep going" in blob
    assert "reading" not in blob
    assert RESUME_MANIFEST_HEADER not in blob


@pytest.mark.anyio
async def test_send_to_llm_resumes_natively(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "spec.md"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # Flushed at the prior result boundary: the first two turns. The trailing
    # tool_result turn was left unflushed (the SEND_TO_LLM tail).
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
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
    # SEND_TO_LLM STREAMS the trailing user turn to kick the next turn: claude-p
    # never self-starts a loaded --resume session, so an empty streamed slice
    # would park it (and the runner in receive_response()) forever.
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    assert streamed[0]["message"]["content"][0]["type"] == "tool_result"
    # The tail was streamed, NOT folded into the native session: the store keeps
    # only the 2 flushed turns, ending on the assistant tool_use.
    reconciled = await store.load(skey)
    assert len(reconciled) == 2
    assert reconciled[-1]["message"]["content"][0]["type"] == "tool_use"


@pytest.mark.anyio
async def test_dispatch_only_tail_selects_fresh_stream(tmp_path: Path) -> None:
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    # Dispatch-only tail: the whole transcript is a single trailing user turn (the
    # rolled-back dispatch). classify_resume -> SEND_TO_LLM; the rewind collapses
    # resume_base to 0.
    dispatch = Message(role="user", content=(TextBlock(text="DISPATCH"),))
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(dispatch)

    # The store has ACCUMULATED from prior respawns: two stale copies of the
    # dispatch. The truncating reconcile must empty it (no session to resume).
    await store.append(skey, message_to_session_records(
        [dispatch, dispatch], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Fresh-stream disposition: no resume= handed to claude-p (unlike the native
    # elif branch, which sets resume=resume_session_id(aid)).
    assert opts.resume is None
    # The truncating reconcile emptied the accumulated store — nothing to resume.
    assert await store.load(skey) is None
    # The full history streams ONCE as the fresh delta (one copy of the dispatch).
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    # An all-TextBlock user turn serializes content to a PLAIN STRING via
    # _join_text (claude_code.py:864-866), not a list of blocks.
    assert streamed[0]["message"]["content"] == "DISPATCH"


@pytest.mark.anyio
async def test_backoff_respawns_do_not_accumulate_store(tmp_path: Path) -> None:
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    dispatch = Message(role="user", content=(TextBlock(text="DISPATCH"),))
    transcript_path = inv.transcript_dir / "transcript.jsonl"

    def factory_of(created: list) -> Any:
        def factory(opts: Any) -> FakeClaudeSDKClient:
            c = FakeClaudeSDKClient(opts, scripted_messages=[
                assistant(tool_use("c1", "complete_step", {"status": "Success"})),
                result(),
            ])
            created.append(c)
            return c
        return factory

    # Simulate N backoff respawns. Before each, the executor's rollback brings the
    # transcript back to the dispatch-only tail (reset it here), and the store has
    # re-accumulated an extra stale copy (as a growing claude-p session would).
    # Each respawn must truncate the store back and stream exactly one copy.
    for cycle in range(1, 4):
        transcript_path.unlink(missing_ok=True)
        Transcript(transcript_path).append(dispatch)

        stale = message_to_session_records(
            [dispatch] * cycle, parent_uuid=None, session_id=skey["session_id"],
            cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0)
        await store.append(skey, stale)

        # Guard: the store really holds the stale body before the runner empties it.
        assert await store.load(skey) is not None

        created: list[FakeClaudeSDKClient] = []
        runner = ClaudeCodeAgentRunner(
            model_name="claude-sonnet-4-5", client_factory=factory_of(created))
        [ev async for ev in runner.run(inv)]

        # Fresh-stream disposition (Task 2): no resume= onto the emptied store.
        assert created[0].opts.resume is None
        # The store is truncated to the empty message prefix — it never grows.
        assert await store.load(skey) is None
        # The streamed delta is exactly one copy of the history each respawn.
        streamed = created[0].queries[0]
        assert len(streamed) == 1 and streamed[0]["type"] == "user"
        # All-TextBlock user turn -> plain string content (see _join_text).
        assert streamed[0]["message"]["content"] == "DISPATCH"


@pytest.mark.anyio
async def test_inplace_repair_resumes_natively_with_store(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "a.txt"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)),
        # Dangling t1 tool_use followed by a persisted interrupt marker:
        # classify_resume returns SEND_TO_LLM (last turn is a user turn).
        Message(role="assistant", content=(
            ToolUseBlock(id="t1", name="mcp__imp__read_file",
                         input={"path": "b.txt"}),)),
        Message(role="user", content=(
            TextBlock(text="[Request interrupted by user]"),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # Flushed at the prior result boundary: the first two turns. The rest is the
    # unflushed tail reconcile must synthesize in.
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # In-place-repair shape WITH a store takes the native path (not fallback).
    # Its last turn is the user interrupt marker, so it classifies as SEND_TO_LLM:
    # native resume STREAMS that trailing user turn to kick the next turn (an empty
    # delta would deadlock). claude-p pairs the dangling t1 from its loaded session
    # — imp injects no synthetic tool_result. Live acceptance: the SEND_TO_LLM
    # round-trip in tests/test_resume_all_shapes_live.py.
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    assert streamed[0]["message"]["content"] == "[Request interrupted by user]"
    # The flushed prefix (through the dangling t1 turn) stays in the native
    # session; only the interrupt marker is streamed, not reconciled in.
    reconciled = await store.load(skey)
    assert len(reconciled) == 4


@pytest.mark.anyio
async def test_inplace_repair_without_store_reconstructs_native(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)
    # No session store → reconstructs from the transcript and goes native.

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "a.txt"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)))
    # Dangling t1 tool_use followed by a persisted interrupt marker:
    # classify_resume returns SEND_TO_LLM (last turn is a user turn).
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="mcp__imp__read_file",
                     input={"path": "b.txt"}),)))
    t.append(Message(role="user", content=(
        TextBlock(text="[Request interrupted by user]"),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
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
    assert opts.session_id is None
    # SEND_TO_LLM STREAMS the trailing user interrupt marker to kick the turn;
    # claude-p pairs the dangling t1 from its (reconstructed) loaded session.
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    assert streamed[0]["message"]["content"] == "[Request interrupted by user]"
    # The flushed prefix (through the dangling t1 turn) was reconstructed into
    # the native session; only the interrupt marker is streamed, not stored.
    reconciled = await store.load(skey)
    assert len(reconciled) == 4


@pytest.mark.anyio
async def test_dangling_tool_use_resumes_natively(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "a.txt"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)),
        # Mid-tool interrupt: a bare assistant tool_use with NO tool_result.
        Message(role="assistant", content=(
            ToolUseBlock(id="t1", name="mcp__imp__read_file",
                         input={"path": "b.txt"}),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # Flushed at the prior result boundary: the first two turns. The tool_result
    # and the dangling t1 turn are the unflushed tail.
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    # The transcript_invalid abort did NOT fire: the run reached completion.
    assert events[-1]["type"] == "agent_output"
    assert not any(
        e["type"] == "error"
        and e["payload"].get("kind") == "transcript_invalid"
        for e in events)
    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None
    # DANGLING native resume STREAMS a synthesized placeholder tool_result for t1
    # to kick the next turn — an empty delta would deadlock (claude-p never
    # self-starts a loaded --resume session). claude-p pairs the dangling t1 from
    # its loaded session against this streamed result.
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    assert streamed[0]["message"]["content"][0]["type"] == "tool_result"
    assert streamed[0]["message"]["content"][0]["tool_use_id"] == "t1"
    # The synth tool_result turn was injected (4 original + 1 synth user turn +
    # 1 new assistant turn = 6).
    assert len(t.load()) == 6
    # The dangling t1 turn stays in the native session as the trailing reconciled
    # entry; the synth result is streamed, not reconciled in.
    reconciled = await store.load(skey)
    assert len(reconciled) == 4
    assert reconciled[-1]["message"]["content"][0] == {
        "type": "tool_use", "id": "t1",
        "name": "mcp__imp__read_file", "input": {"path": "b.txt"}}


@pytest.mark.anyio
async def test_dangling_tool_use_without_store_reconstructs_native(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)
    # No session store → reconstructs from the transcript and goes native.

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "a.txt"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)))
    # Mid-tool interrupt: a bare assistant tool_use with NO tool_result.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="mcp__imp__read_file",
                     input={"path": "b.txt"}),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    # The transcript_invalid abort did NOT fire: the run reached completion.
    assert events[-1]["type"] == "agent_output"
    assert not any(
        e["type"] == "error"
        and e["payload"].get("kind") == "transcript_invalid"
        for e in events)
    opts = created[0].opts
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None
    # DANGLING native resume STREAMS a synthesized placeholder tool_result for t1.
    streamed = created[0].queries[0]
    assert len(streamed) == 1 and streamed[0]["type"] == "user"
    assert streamed[0]["message"]["content"][0]["type"] == "tool_result"
    assert streamed[0]["message"]["content"][0]["tool_use_id"] == "t1"
    # The dangling t1 turn was reconstructed as the trailing native-session entry;
    # the synth result is streamed, not stored.
    reconciled = await store.load(skey)
    assert len(reconciled) == 4
    assert reconciled[-1]["message"]["content"][0] == {
        "type": "tool_use", "id": "t1",
        "name": "mcp__imp__read_file", "input": {"path": "b.txt"}}



@pytest.mark.anyio
async def test_fresh_run_isolates_config_dir_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh run materializes an isolated CLAUDE_CONFIG_DIR (with projects/),
    drops the suppression flag, and removes the dir after a successful turn."""
    (tmp_path / "art").mkdir()
    seed_src = tmp_path / "seed-home"
    seed_src.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(seed_src))
    captured: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        cfg = opts.env.get("CLAUDE_CONFIG_DIR")
        captured["cfg"] = cfg
        captured["had_projects_during_run"] = bool(cfg) and Path(cfg, "projects").is_dir()
        captured["skip_history_present"] = "CLAUDE_CODE_SKIP_PROMPT_HISTORY" in opts.env
        return FakeClaudeSDKClient(opts, scripted_messages=[
            assistant(
                text("done"),
                tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
            ),
            result(),
        ])

    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5", client_factory=factory)
    _ = [ev async for ev in runner.run(_inv(tmp_path))]

    assert captured["cfg"], "fresh run must set CLAUDE_CONFIG_DIR"
    assert captured["had_projects_during_run"], "isolated dir needs a projects/ subdir"
    assert captured["skip_history_present"] is False, "suppression flag must be dropped"
    assert not Path(captured["cfg"]).exists(), "config dir must be removed after the run"


@pytest.mark.anyio
async def test_fresh_run_cleans_config_dir_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cleanup lives in run()'s finally, so an interrupt still removes the dir."""
    (tmp_path / "art").mkdir()
    seed_src = tmp_path / "seed-home"
    seed_src.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(seed_src))
    captured: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        captured["cfg"] = opts.env.get("CLAUDE_CONFIG_DIR")
        return FakeClaudeSDKClient(opts, scripted_messages=[])

    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5", client_factory=factory)
    await runner.interrupt()  # the SIGINT-driven cancel that already fired
    _ = [ev async for ev in runner.run(_inv(tmp_path))]

    assert captured["cfg"]
    assert not Path(captured["cfg"]).exists()


def test_build_options_requests_1m_via_model_id_for_opus_1m(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="opus-1m")
    opts = runner._build_options(_inv(tmp_path), mcp_server=object(), tool_names=[])
    # No custom beta (subscription auth drops it + warns); the 1M window is
    # requested via the bracketed model-id, which both auth modes honor.
    assert list(opts.betas) == []
    assert opts.model == "claude-opus-4-8[1m]"


def test_build_options_plain_opus_has_no_betas(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="opus")
    opts = runner._build_options(_inv(tmp_path), mcp_server=object(), tool_names=[])
    assert list(opts.betas) == []
    assert opts.model == "opus"


def test_high_tier_floor_defaults_to_opus_1m() -> None:
    from imp.providers.claude_code import TIER_DEFAULTS
    assert TIER_DEFAULTS["high"] == "opus-1m"


@pytest.mark.anyio
async def test_llm_response_enriched_with_context_usage(tmp_path: Path) -> None:
    from tests.cc_fakes import context_usage

    scripted = [
        assistant(
            text("done"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(
            opts, scripted_messages=scripted,
            context_usage=context_usage(
                raw_max_tokens=1_000_000, percentage=4.2, total_tokens=42_000),
        )

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    runner = ClaudeCodeAgentRunner(model_name="opus-1m", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert resp["payload"]["context_window"] == 1_000_000
    assert resp["payload"]["context_pct"] == 4.2
    assert resp["payload"]["context_tokens"] == 42_000


@pytest.mark.anyio
async def test_context_usage_failure_does_not_break_run(tmp_path: Path) -> None:
    scripted = [
        assistant(
            text("done"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(
            opts, scripted_messages=scripted, context_usage_raises=True)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    runner = ClaudeCodeAgentRunner(model_name="opus-1m", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    assert [e["type"] for e in events] == ["agent_output"]
    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert "context_window" not in resp["payload"]
    assert "context_pct" not in resp["payload"]
    assert "context_tokens" not in resp["payload"]


def test_run_state_has_no_wait_for_user_pause_fields() -> None:
    """No live-run wait_for_user pause intercept; the _build_tools carve-out is removed."""
    import dataclasses
    import inspect

    import imp.providers.claude_code as cc_mod
    from imp.providers.claude_code import _RunState

    names = {f.name for f in dataclasses.fields(_RunState)}
    assert "awaiting_user" not in names
    assert "awaiting_prompt" not in names

    src = inspect.getsource(cc_mod)
    assert 'b.name == "wait_for_user"' not in src
    assert 'if name == "wait_for_user"' not in src


@pytest.mark.anyio
async def test_cooperative_interrupt_steer_continues_no_pause(
    tmp_path: Path,
) -> None:
    """An attended agent interrupted mid-turn, given a redirect, acts on it and
    continues on the SAME live client — no interrupted error, no disconnect(),
    no PAUSED left for the daemon."""
    import asyncio

    from imp.cli.signals import InterruptCoordinator
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    coord = InterruptCoordinator()
    after_steer = [
        assistant(text("checking the other file"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, CooperativeClient] = {}

    def factory(opts: Any) -> CooperativeClient:
        c = CooperativeClient(opts, after_steer=after_steer)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=lambda _prompt: "go check the other file instead")
    inv = replace(_inv(tmp_path), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    events: list[dict[str, Any]] = []

    async def drive() -> None:
        async for ev in runner.run(inv):
            events.append(ev)

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await coord.signal_pause()
    await asyncio.wait_for(task, timeout=2)

    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["status"] == "Success"
    assert not any(e.get("payload", {}).get("kind") == "interrupted"
                   for e in events if e["type"] == "error")
    assert holder["c"].disconnected is False
    # Two interrupts: the cooperative steer (#1), then Part A's completion-interrupt
    # preempting the steered turn that emits complete_step (#2).
    assert holder["c"].interrupt_calls == 2
    assert coord.should_pause is False
    assert holder["c"].queries[-1] == "go check the other file instead"


@pytest.mark.anyio
async def test_cooperative_interrupt_headless_pauses_balanced(
    tmp_path: Path,
) -> None:
    """No user_input_fn: the cooperative stop ends the turn balanced, then
    raises AwaitingUserInput — never a disconnect() (the session stays balanced
    so the resume continues with no 400)."""
    import asyncio

    from imp.cli.signals import InterruptCoordinator
    from imp.engine.awaiting_user_input import AwaitingUserInput
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    coord = InterruptCoordinator()
    after_steer = [assistant(text("unused")), result()]
    holder: dict[str, CooperativeClient] = {}

    def factory(opts: Any) -> CooperativeClient:
        c = CooperativeClient(opts, after_steer=after_steer)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    inv = replace(_inv(tmp_path), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    async def drive() -> None:
        async for _ev in runner.run(inv):
            pass

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await coord.signal_pause()
    with pytest.raises(AwaitingUserInput):
        await asyncio.wait_for(task, timeout=2)

    assert holder["c"].disconnected is False
    assert holder["c"].interrupt_calls == 1


@pytest.mark.anyio
async def test_inflight_tool_interrupt_orders_interrupt_before_reap(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """AC7: on the cooperative path client.interrupt() fires BEFORE any
    subprocess kill; the orphaned in-flight tool forwards no result, leaked
    subprocesses are reaped only after the turn closes, and the steer — not a
    stale tool result — drives the next turn."""
    import asyncio

    from imp.cli.signals import InterruptCoordinator
    from tests.cc_fakes import CooperativeClient, assistant, result, text, tool_use

    (tmp_path / "art").mkdir()
    order: list[str] = []

    async def _term_run(_ctx: Any) -> None:
        order.append("terminate_running")

    async def _term_bg(_ctx: Any) -> None:
        order.append("terminate_bg")

    monkeypatch.setattr(
        "imp.agents.tools.context.terminate_running_processes", _term_run)
    monkeypatch.setattr(
        "imp.agents.tools.bg.terminate_background_processes", _term_bg)

    coord = InterruptCoordinator()
    after_steer = [
        assistant(text("took the redirect"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, CooperativeClient] = {}

    def factory(opts: Any) -> CooperativeClient:
        c = CooperativeClient(opts, after_steer=after_steer, order=order)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=lambda _prompt: "go look elsewhere")
    inv = replace(_inv(tmp_path), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    events: list[dict[str, Any]] = []

    async def drive() -> None:
        async for ev in runner.run(inv):
            events.append(ev)

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await coord.signal_pause()
    await asyncio.wait_for(task, timeout=2)

    # client.interrupt() preceded every subprocess reap.
    assert order.index("interrupt") < order.index("terminate_running")
    assert order.index("interrupt") < order.index("terminate_bg")
    # The steer, not a stale tool result, drives the next turn.
    assert holder["c"].queries[-1] == "go look elsewhere"
    assert events[-1]["type"] == "agent_output"


@pytest.mark.anyio
async def test_cooperative_steer_drive_invocation_not_paused(
    tmp_path: Path,
) -> None:
    """AC3 integration: drive_invocation returns non-paused after a cooperative
    steer — the daemon layer never sees a PAUSED result, so _hold_prompt never
    shows the re-prompt for an attended steer."""
    import asyncio

    from imp.cli.signals import InterruptCoordinator
    from imp.engine.agent_run import drive_invocation
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    coord = InterruptCoordinator()
    after_steer = [
        assistant(text("taking the redirect"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, CooperativeClient] = {}

    def factory(opts: Any) -> CooperativeClient:
        c = CooperativeClient(opts, after_steer=after_steer)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=lambda _prompt: "redirect here")
    inv = replace(_inv(tmp_path), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    result_holder: dict[str, Any] = {}

    async def do_drive() -> None:
        result_holder["r"] = await drive_invocation(
            inv, runner, coordinator=coord, log=None, emit_events=False)

    task = asyncio.create_task(do_drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await coord.signal_pause()
    await asyncio.wait_for(task, timeout=2)

    r = result_holder["r"]
    assert r.paused is False
    assert r.output == {"status": "Success"}


@pytest.mark.anyio
async def test_second_interrupt_during_steer_skips_requery(
    tmp_path: Path,
) -> None:
    """A second interrupt that fires _hard_interrupt while the run is blocked
    in await steer() sets _cancel=True. The guard after steer() returns must
    exit cleanly without re-querying the now-disconnected client."""
    import asyncio

    from imp.cli.signals import InterruptCoordinator
    from tests.cc_fakes import CooperativeClient

    (tmp_path / "art").mkdir()
    coord = InterruptCoordinator()
    after_steer = [assistant(text("unused")), result()]
    holder: dict[str, CooperativeClient] = {}

    def factory(opts: Any) -> CooperativeClient:
        c = CooperativeClient(opts, after_steer=after_steer)
        holder["c"] = c
        return c

    runner_ref: dict[str, Any] = {}

    def _input_fn(_prompt: str) -> str:
        # Simulate a second interrupt arriving while blocked in the steer:
        # set _cancel directly (what _hard_interrupt does) and return a reply.
        runner_ref["r"]._cancel = True
        return "redirect"

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=_input_fn)
    runner_ref["r"] = runner
    inv = replace(_inv(tmp_path), coordinator=coord)
    coord.attach_runner(runner.interrupt)

    events: list[dict[str, Any]] = []

    async def drive() -> None:
        async for ev in runner.run(inv):
            events.append(ev)

    task = asyncio.create_task(drive())
    for _ in range(1000):
        if "c" in holder:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    await coord.signal_pause()
    await asyncio.wait_for(task, timeout=2)

    # Guard fired: no second query() after the steer.
    # The client had only the initial query() — no re-query after _cancel.
    assert len(holder["c"].queries) == 1
    assert runner._cancel is True


@pytest.mark.anyio
async def test_native_resume_reconcile_idempotent_when_complete(tmp_path: Path) -> None:
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="done early"),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # Store is complete but carries a stale trailing error turn reconcile must strip.
    full = message_to_session_records(
        msgs, parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0)
    await store.append(skey, full)
    stale_error = {"uuid": "stale-error-uuid", "isApiErrorMessage": True}
    await store.append(skey, [stale_error])

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: FakeClaudeSDKClient(opts, scripted_messages=scripted),
        user_input_fn=lambda _prompt: "keep going")
    [ev async for ev in runner.run(inv)]

    # Reconcile-and-overwrite ran but changed nothing: same uuids, no duplication.
    reconciled = await store.load(skey)
    assert [r["uuid"] for r in reconciled] == [r["uuid"] for r in full]
    assert len(reconciled) == 2


@pytest.mark.anyio
async def test_native_resume_reconciles_partial_store(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        session_key,
    )

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "a.txt"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)),
        Message(role="assistant", content=(TextBlock(text="done early"),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # Store holds only the first turn; the rest were unflushed at termination.
    await store.append(skey, message_to_session_records(
        msgs[:1], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: FakeClaudeSDKClient(opts, scripted_messages=scripted),
        user_input_fn=lambda _prompt: "keep going")
    [ev async for ev in runner.run(inv)]

    # The three trailing turns missing from the store were synthesized in; the
    # streamed reply turn is NOT stored (it rides messages[resume_base:]).
    reconciled = await store.load(skey)
    assert len(reconciled) == 4
    assert reconciled[-1]["type"] == "assistant"
    assert reconciled[-1]["message"]["content"][0]["text"] == "done early"


@pytest.mark.anyio
async def test_no_store_native_sets_standard_tool_mode(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    # No session store → reconstructs and goes native. The SEND_TO_LLM tail
    # is native-eligible; standard tool mode is imp's default on every path, so
    # ENABLE_TOOL_SEARCH=false is set here too.
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Confirm this really is the native path (reconstructed, not replay fallback).
    assert opts.resume == resume_session_id("step:p/s")
    # Standard tool mode is the default on every path: the env knob is set.
    assert opts.env.get("ENABLE_TOOL_SEARCH") == "false"


@pytest.mark.anyio
async def test_native_resume_sets_standard_tool_mode(tmp_path: Path) -> None:
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    # A prior fresh run mirrored its session into the store → native resume.
    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)))
    # Bare turn-end → AWAITING_USER → native resume (store has the body).
    t.append(Message(role="assistant", content=(TextBlock(text="done early"),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=lambda _prompt: "keep going")
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Confirm this really is the native path.
    assert opts.resume == resume_session_id("step:p/s")
    # Standard tool mode is imp's default on every path, including native
    # resume: the env knob is set.
    assert opts.env.get("ENABLE_TOOL_SEARCH") == "false"


@pytest.mark.anyio
async def test_complete_step_tail_native_resumes_with_store_body(
        tmp_path: Path) -> None:
    """A resume whose tail is a `complete_step` tool_use takes the NATIVE path even
    when a store body is present (the needs_imp_balance veto is gone): imp folds a
    SUCCESS result into the loaded session via synthesize_native_success_on_resume,
    so claude-p resumes its own session by id rather than re-streaming a replay."""
    from imp.agents.runner import ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    # A session store is present — but a trailing complete_step forces the replay
    # path regardless, so ENABLE_TOOL_SEARCH=false must still be set.
    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    # Tail is a successful complete_step awaiting its result → imp must balance.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="complete_step",
                     input={"status": "Success"}),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # complete_step tail now NATIVE-resumes (native_resume=True).
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None
    # Standard tool mode is imp's default on every path, native resume included.
    assert opts.env.get("ENABLE_TOOL_SEARCH") == "false"


async def _seed_drifted_store(inv: AgentInvocation, n_turns: int) -> None:
    """Seed a session store whose body has MORE conversation turns than the
    transcript, forcing store_reconcilable(...) False -> the replay fallback."""
    from imp.providers.claude_code_session_store import session_key, session_store_for

    store = session_store_for(inv.transcript_dir)
    body = [
        {"type": "user" if i % 2 == 0 else "assistant", "uuid": f"d{i}",
         "message": {"role": "user" if i % 2 == 0 else "assistant", "content": "x"}}
        for i in range(n_turns)
    ]
    await store.append(session_key(inv.change_context.project_root, inv.agent_id), body)


@pytest.mark.anyio
async def test_unreconstructable_store_falls_back_to_replay(tmp_path: Path) -> None:
    """When the store body has drifted ahead of the transcript, the resume can no
    longer native-resume: it takes the replay disposition (no loaded session)."""
    inv = _seed_complete_step_resume(tmp_path, retry_feedback="REVISE: rewrite plan.md")
    await _seed_drifted_store(inv, 4)  # 4 body turns > 2 transcript msgs
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []
    captured: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        cfg = opts.env.get("CLAUDE_CONFIG_DIR")
        captured["cfg"] = cfg
        captured["cfg_exists_during_run"] = bool(cfg) and Path(cfg).is_dir()
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    # Replay disposition: no native session handed to claude-p.
    assert created[0].opts.resume is None
    assert created[0].opts.session_id is None
    assert created[0].opts.session_store is None
    # Replay path must isolate claude-p in an ephemeral CLAUDE_CONFIG_DIR.
    assert captured["cfg"], "replay must set CLAUDE_CONFIG_DIR"
    assert captured["cfg_exists_during_run"], "ephemeral dir must exist during the run"
    assert not Path(captured["cfg"]).exists(), "config dir must be removed after the run"


@pytest.mark.anyio
async def test_balance_on_resume_send_to_llm_tail_native_resumes(
        tmp_path: Path) -> None:
    """A crash/pause resume (balance_on_resume=True) of a SEND_TO_LLM tail that has
    a store body must NATIVE-resume, not fall back to stream-json replay. The
    blanket balance_on_resume veto used to force replay here, which 400s on the
    deferred MCP tool_use blocks in the re-streamed history. Native resume only
    needs imp-side tail balancing when the tail is a complete_step (success
    override) or carries retry_feedback — neither holds here."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.providers.claude_code_session_store import session_key

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
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)))
    # SEND_TO_LLM tail (ends on a balanced user tool_result); no complete_step,
    # no retry_feedback → native resume is correct.

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})),
                result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    opts = created[0].opts
    # Native path: claude-p loads its own session — no stream-json replay, no 400.
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_store is not None
    # Standard tool mode is imp's default on every path, native resume included.
    assert opts.env.get("ENABLE_TOOL_SEARCH") == "false"


@pytest.mark.anyio
async def test_send_to_llm_tail_with_feedback_native_resumes_streaming_handback(
        tmp_path: Path) -> None:
    """A SEND_TO_LLM resume carrying retry_feedback now takes the NATIVE path (the
    needs_imp_balance veto is gone); the handback rides the streamed delta alongside
    the trailing tool_result, so it is never lost."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True,
                  retry_feedback="REVISE: tighten the plan.")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    store = session_store_for(inv.transcript_dir)
    await store.append(
        session_key(inv.change_context.project_root, "step:p/s"),
        [{"type": "user", "uuid": "m0",
          "message": {"role": "user", "content": "go"}}])

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    # Native resume (no replay): the loaded session is resumed by id.
    assert created[0].opts.resume == resume_session_id("step:p/s")
    assert created[0].opts.session_id is None
    # The handback rides the streamed delta.
    blob = _user_text_blob(created[0].queries[0])
    assert "REVISE: tighten the plan." in blob
    # No fallback manifest on the native path.
    assert RESUME_MANIFEST_HEADER not in blob


@pytest.mark.anyio
async def test_balance_on_resume_no_store_reconstructs_native(tmp_path: Path) -> None:
    """A production resume (balance_on_resume=True) with NO store and a
    SEND_TO_LLM tail now reconstructs the session and resumes natively.
    balance_on_resume does NOT veto native resume, so no stream-json replay and
    no redo-defusing manifest are produced."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import RESUME_MANIFEST_HEADER, Transcript
    from imp.providers.claude_code_session_store import session_key

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file",
                     input={"path": "spec.md"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="SPECBODY", is_error=False),)))

    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
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
    assert opts.session_id is None
    # No stream-json replay → no redo-defusing manifest in the transcript.
    replayed = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert not any(
        any(isinstance(b, TextBlock) and b.text.startswith(RESUME_MANIFEST_HEADER)
            for b in m.content)
        for m in replayed if m.role == "user")
    # The 2 flushed turns were reconstructed; the trailing user tool_result is
    # streamed as the SEND_TO_LLM delta, not stored.
    reconciled = await store.load(skey)
    assert len(reconciled) == 2


@pytest.mark.anyio
async def test_coalesces_one_llm_response_per_message_id(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        # turn m1 streamed as three fragments sharing one message_id
        assistant(thinking("planning"), message_id="m1", model="served-x"),
        assistant(text("reading the files"), message_id="m1", model="served-x"),
        assistant(tool_use("u1", "read_file", {"path": "a"}),
                  tool_use("u2", "grep", {"pattern": "x"}),
                  message_id="m1", model="served-x", stop_reason="tool_use"),
        user(tool_result("u1", "data-a"), tool_result("u2", "data-b")),
        # turn m2: prose + complete_step
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m2", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn"),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log,
               tools=frozenset({"complete_step", "read_file", "grep"}))
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    resps = [e for e in log.read() if e["type"] == "llm_response"]
    # AC1/AC3: exactly one llm_response per distinct message_id
    assert [r["payload"]["message_id"] for r in resps] == ["m1", "m2"]
    m1 = resps[0]["payload"]
    # AC1: all the group's blocks, in order, in one event
    assert [b["type"] for b in m1["raw"]["content"]] == \
        ["thinking", "text", "tool_use", "tool_use"]
    # AC1/AC4: real stop_reason (never null), served model from the fragment
    assert m1["stop_reason"] == "tool_use"
    assert m1["model"] == "served-x"          # served, NOT the configured model
    assert m1["raw"]["stop_reason"] == "tool_use"
    m2 = resps[1]["payload"]
    assert [b["type"] for b in m2["raw"]["content"]] == ["text", "tool_use"]
    assert m2["stop_reason"] == "end_turn"
    assert m2["model"] == "served-x"
    # AC2: N tool_use blocks -> N tool_call events and N paired tool_result events
    tcs = [e for e in log.read() if e["type"] == "tool_call"
           and e["payload"]["name"] in ("read_file", "grep")]
    trs = [e for e in log.read() if e["type"] == "tool_result"
           and e["payload"]["id"] in ("u1", "u2")]
    assert len(tcs) == 2
    assert len(trs) == 2


@pytest.mark.anyio
async def test_none_message_id_folds_into_open_group(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(text("part one"), message_id="m1", model="served-x"),
        # a continuation fragment whose message_id is None must fold into m1,
        # never open a new group keyed on None
        assistant(text(" part two"), message_id=None, model="served-x"),
        assistant(tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m1", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn"),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    resps = [e for e in log.read() if e["type"] == "llm_response"]
    assert len(resps) == 1                      # one group, not two
    assert resps[0]["payload"]["message_id"] == "m1"
    texts = [b.get("text") for b in resps[0]["payload"]["raw"]["content"]
             if b["type"] == "text"]
    assert texts == ["part one", " part two"]   # the None fragment continued m1


@pytest.mark.anyio
async def test_none_message_id_opens_group_then_adopts_real_key(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        # group OPENS on a None-keyed fragment; the next non-None fragment must
        # upgrade the group key to "m1" rather than leaving it None-keyed.
        assistant(text("part one"), message_id=None, model="served-x"),
        assistant(text(" part two"), message_id="m1", model="served-x"),
        assistant(tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m1", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn"),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    resps = [e for e in log.read() if e["type"] == "llm_response"]
    assert len(resps) == 1                      # one group, not two
    assert resps[0]["payload"]["message_id"] == "m1"   # adopted the real key
    texts = [b.get("text") for b in resps[0]["payload"]["raw"]["content"]
             if b["type"] == "text"]
    assert texts == ["part one", " part two"]


@pytest.mark.anyio
async def test_emits_one_query_result_with_authoritative_telemetry(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m1", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn", subtype="success",
               total_cost_usd=0.0321, duration_ms=5000, duration_api_ms=4100,
               num_turns=1, usage={"input_tokens": 900, "output_tokens": 120},
               model_usage={"served-x": {"input_tokens": 900}}),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    qrs = [e for e in log.read() if e["type"] == "query_result"]
    assert len(qrs) == 1                         # AC5: exactly one per query
    p = qrs[0]["payload"]
    assert p["total_cost_usd"] == 0.0321
    assert p["duration_ms"] == 5000
    assert p["duration_api_ms"] == 4100
    assert p["num_turns"] == 1
    assert p["stop_reason"] == "end_turn"
    assert p["subtype"] == "success"
    assert p["is_error"] is False
    assert p["usage"] == {"input_tokens": 900, "output_tokens": 120}
    assert p["model_usage"] == {"served-x": {"input_tokens": 900}}


@pytest.mark.anyio
async def test_query_result_carries_optional_error_fields(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m1", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn", subtype="success",
               permission_denials=[{"tool": "bash"}], errors=["boom"]),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    p = next(e for e in log.read() if e["type"] == "query_result")["payload"]
    assert p["permission_denials"] == [{"tool": "bash"}]
    assert p["errors"] == ["boom"]


@pytest.mark.anyio
async def test_turn_count_matches_message_ids_and_num_turns(tmp_path: Path) -> None:
    from tests.cc_fakes import tool_result, user
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(tool_use("u1", "read_file", {"path": "a"}),
                  message_id="m1", model="served-x", stop_reason="tool_use"),
        user(tool_result("u1", "data")),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m2", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn", num_turns=2),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log,
               tools=frozenset({"complete_step", "read_file"}))
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    resps = [e for e in log.read() if e["type"] == "llm_response"]
    qr = next(e for e in log.read() if e["type"] == "query_result")
    distinct_ids = {r["payload"]["message_id"] for r in resps}
    # AC3: llm_response count == distinct message_id count == num_turns
    assert len(resps) == len(distinct_ids) == qr["payload"]["num_turns"] == 2


@pytest.mark.anyio
async def test_claude_code_impl_records_tool_duration(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    import imp.providers.claude_code as cc_mod
    from imp.agents.tools.context import ToolContext

    async def _noop(_ctx: Any) -> None:
        return None
    monkeypatch.setattr(cc_mod._bg, "_sweep_bg", _noop)

    target = tmp_path / "f.txt"
    target.write_text("hello")
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, tools=frozenset({"read_file"}))
    state = cc_mod._RunState()
    state.agent_id = "step:p/s"
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl",
                      event_log=log, agent_id="step:p/s")

    captured: dict[str, Any] = {}

    def sdk_tool(name: str, desc: str, schema: Any, annotations: Any = None) -> Any:
        def deco(fn: Any) -> Any:
            captured["impl"] = fn
            return fn
        return deco

    tool = cc_mod._build_tools(inv)["read_file"]
    cc_mod._wrap_one("read_file", tool, ctx, state, sdk_tool)
    await captured["impl"]({"path": str(target), "__tool_use_id__": "u9"})

    assert "u9" in state.tool_durations
    assert 0 <= state.tool_durations["u9"] < 5_000


@pytest.mark.anyio
async def test_claude_code_tool_result_carries_recorded_duration(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(tool_use("u1", "read_file", {"path": "a"}),
                  message_id="m1", model="served-x", stop_reason="tool_use"),
        user(tool_result("u1", "file body")),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m2", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn"),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log,
               tools=frozenset({"complete_step", "read_file"}))
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    captured_calls: list[Any] = []
    orig = log.append_tool_result

    def spy(**kwargs: Any) -> Any:
        captured_calls.append(kwargs)
        return orig(**kwargs)
    log.append_tool_result = spy  # type: ignore[method-assign]

    async for _ev in runner.run(inv):
        pass

    u1 = next(k for k in captured_calls
              if getattr(k["block"], "tool_use_id", None) == "u1")
    assert "duration_ms" in u1                  # keyword always passed; None when unrun


@pytest.mark.anyio
async def test_claude_code_tool_result_duration_non_none_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.providers.claude_code as cc_mod
    orig_cls = cc_mod._RunState
    def _seeded_state():
        s = orig_cls()
        s.tool_durations["u1"] = 99.0
        return s
    monkeypatch.setattr(cc_mod, "_RunState", _seeded_state)

    (tmp_path / "art").mkdir()
    scripted = [
        assistant(tool_use("u1", "read_file", {"path": "a"}),
                  message_id="m1", model="served-x", stop_reason="tool_use"),
        user(tool_result("u1", "file body")),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"}),
                  message_id="m2", model="served-x", stop_reason="end_turn"),
        result(stop_reason="end_turn"),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log,
               tools=frozenset({"complete_step", "read_file"}))
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    async for _ev in runner.run(inv):
        pass

    tr = next(
        e for e in log.read()
        if e["type"] == "tool_result" and e["payload"]["id"] == "u1"
    )
    assert tr["payload"]["duration_ms"] == 99.0


def test_build_options_enables_partial_messages(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    opts = runner._build_options(_inv(tmp_path), object(), ["complete_step"])
    assert opts.include_partial_messages is True


@pytest.mark.anyio
async def test_receive_loop_pulses_note_activity_per_message(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    (tmp_path / "art").mkdir()
    scripted = [
        assistant(
            text("calling complete_step"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
        result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    coord = InterruptCoordinator()
    pulses: list[int] = []
    coord.add_activity_listener(lambda: pulses.append(1))

    inv = replace(_inv(tmp_path), coordinator=coord)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=fake_client_factory,
    )
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"
    # receive_response() yields the assistant message then the ResultMessage —
    # two received SDK messages, so two liveness pulses for this single turn.
    assert len(pulses) == 2


@pytest.mark.anyio
async def test_subagent_bare_turn_end_resume_returns_deliverable(
        tmp_path: Path) -> None:
    """A subagent resumed from a bare-turn-end terminal re-emits its final text
    as the deliverable and returns — it does NOT native-resume (handing a
    finished subagent back to claude-p) or solicit the human."""
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = replace(_inv(tmp_path, agent_id="sub:tc-1"), subagent_depth=1)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant",
                     content=(TextBlock(text="the conclusion"),)))

    created: list[Any] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=[])
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    yielded = [ev async for ev in runner.run(inv)]

    assert [e["type"] for e in yielded] == ["agent_output"]
    assert yielded[-1]["payload"] == {"text": "the conclusion"}
    # Short-circuited before any SDK client / native-resume session was built.
    assert created == []


def _seed_write_then_complete_resume(
    tmp_path: Path, *, retry_feedback: str | None, log: InMemoryEventLog,
) -> AgentInvocation:
    """Transcript: go -> write_file(w0) -> tool_result(w0 ok) -> complete_step(c0).
    A prior SUCCEEDED write gives the manifest a non-control call to name."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True, retry_feedback=retry_feedback)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="w0", name="mcp__imp__write_file",
                     input={"path": "plan.md", "content": "PLAN BODY"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="w0", content="ok", is_error=False),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="c0", name="complete_step", input={"status": "Success"}),)))
    return inv


@pytest.mark.anyio
async def test_replay_fallback_manifest_folds_handback_no_false_errors(
        tmp_path: Path) -> None:
    """On the replay fallback the per-tool SUCCEEDED manifest is emitted (even on
    the balance path), the handback is folded into that same composed turn, and no
    reconstructed tool_result is streamed as an error."""
    from imp.agents.transcript import RESUME_MANIFEST_HEADER

    log = InMemoryEventLog()
    inv = _seed_write_then_complete_resume(
        tmp_path, retry_feedback="REVISE: rewrite plan.md", log=log)
    await _seed_drifted_store(inv, 8)  # 8 body turns > 4 transcript msgs -> replay
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    assert created[0].opts.resume is None  # replay disposition
    delta = created[0].queries[0]
    blob = _user_text_blob(delta)
    assert RESUME_MANIFEST_HEADER in blob
    assert "write_file — succeeded" in blob
    assert "REVISE: rewrite plan.md" in blob
    assert not any(
        d.get("type") == "user" and isinstance(d["message"]["content"], list)
        and any(b.get("type") == "tool_result" for b in d["message"]["content"])
        for d in delta)


@pytest.mark.anyio
async def test_replay_fallback_streams_one_composed_user_turn(tmp_path: Path) -> None:
    """Prior history is collapsed to a single composed user turn — the multiple
    prior transcript turns are NOT streamed as N separate stream-json user lines."""
    log = InMemoryEventLog()
    inv = _seed_write_then_complete_resume(
        tmp_path, retry_feedback="REVISE: rewrite plan.md", log=log)
    await _seed_drifted_store(inv, 8)
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    delta = created[0].queries[0]
    user_turns = [d for d in delta if d.get("type") == "user"]
    assert len(user_turns) == 1
    assert "go" not in _user_text_blob(delta)


@pytest.mark.anyio
async def test_replay_fallback_excludes_dangling_from_manifest(tmp_path: Path) -> None:
    """A genuinely dangling (interrupted) trailing tool_use is NOT claimed
    succeeded in the manifest, while a prior real success still is."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    inv = replace(inv, balance_on_resume=True, retry_feedback="REVISE: keep going")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="w0", name="mcp__imp__write_file",
                     input={"path": "plan.md", "content": "PLAN BODY"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="w0", content="ok", is_error=False),)))
    t.append(Message(role="assistant", content=(   # dangling: no result follows
        ToolUseBlock(id="b0", name="mcp__imp__bash", input={"command": "ls"}),)))
    await _seed_drifted_store(inv, 8)
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    blob = _user_text_blob(created[0].queries[0])
    assert "write_file — succeeded" in blob
    assert "bash — succeeded" not in blob


@pytest.mark.anyio
async def test_replay_fallback_empty_delta_kicked_with_redo_nudge(tmp_path: Path) -> None:
    """A bare replay resume with no prior tool calls and no handback still streams a
    non-empty delta (a redo nudge), so the delta-only stream never deadlocks."""
    from imp.agents.transcript import RESUME_REDO_NUDGE

    inv = _seed_complete_step_resume(tmp_path, retry_feedback=None)
    await _seed_drifted_store(inv, 4)
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    assert created[0].opts.resume is None  # replay disposition
    assert _user_text_blob(created[0].queries[0]).strip() == RESUME_REDO_NUDGE


@pytest.mark.anyio
async def test_replay_fallback_emits_runner_warning(tmp_path: Path) -> None:
    """On fallback entry a runner_warning names the resume shape and why native was
    unavailable; it is durable in the event log."""
    log = InMemoryEventLog()
    inv = _seed_write_then_complete_resume(
        tmp_path, retry_feedback="REVISE: rewrite plan.md", log=log)
    await _seed_drifted_store(inv, 8)
    scripted = [assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    [ev async for ev in runner.run(inv)]

    warnings = [e["payload"]["message"] for e in log.read()
                if e["type"] == "runner_warning"]
    assert warnings, "expected a runner_warning on fallback entry"
    assert any(
        "replay" in m and "unreconstructable" in m and "dangling_tool_use" in m
        for m in warnings), warnings
