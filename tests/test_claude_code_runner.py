# tests/test_claude_code_runner.py
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner
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
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
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
    assert len(loaded) == 4  # 3 original + 1 new assistant turn


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
async def test_interactive_resume_from_prompt_user_prompts_then_continues(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use

    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, event_log=log, agent_id="step:p/s")
    inv = replace(inv, interactive=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(TextBlock(text="confirm?"),),
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

    # Verify ordering in the event log: user_message before llm_response
    log_events = list(log.read())
    log_types = [e["type"] for e in log_events]
    user_idx = log_types.index("user_message")
    llm_idx = log_types.index("llm_response")
    assert user_idx < llm_idx, (
        f"user_message (idx={user_idx}) must precede llm_response (idx={llm_idx})"
    )

    loaded = t.load()
    assert len(loaded) >= 3
    assert loaded[2].role == "user"
    assert isinstance(loaded[2].content[0], TextBlock)
    assert loaded[2].content[0].text == "yes, proceed"


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
async def test_interactive_requery_across_two_receive_rounds_no_error(tmp_path: Path) -> None:
    """An interactive turn ending without complete_step re-queries; turn 2 calls
    complete_step. With per-turn receive_response() this must NOT surface a
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
    inv = replace(inv, interactive=True,
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
    step = Step(name="s", phase="p", kind="agent", rules=(), options=(),
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
