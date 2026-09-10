# tests/test_codex_cancel_paths.py
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_git_and_auth(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    auth = tmp_path / "auth.json"
    auth.write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)
    return auth


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC, tier=None)


def _invocation(tmp_path: Path, *, event_log: object,
                continuation_fn: object = None,
                user_input_fn: object = None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(
            title="t", project_root=tmp_path, workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl", agent_id="step:t",
        event_log=event_log, continuation_fn=continuation_fn,
        user_input_fn=user_input_fn)


@pytest.mark.anyio
async def test_silent_stream_end_after_cancel_yields_interrupted(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    # An empty scripted turn: the event loop body never runs at all, so the
    # in-loop cancel check can't fire — the post-loop recheck must catch it.
    # on_turn_start fires before any events are yielded; with an empty event
    # list the inner `async for` body is never entered, so only the post-loop
    # recheck can produce the interrupted event.
    scripted = [[]]  # single empty turn — inner loop body never executes

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(
            scripted_turns=scripted,
            on_turn_start=lambda: setattr(runner, "_cancel", True)))
    runner._canonical_auth_override = auth

    events = [ev async for ev in runner.run(_invocation(tmp_path, event_log=elog))]

    interrupted = [e for e in events if e.get("payload", {}).get("kind") == "interrupted"]
    assert interrupted, events


@pytest.mark.anyio
async def test_cancel_after_continuation_resolves_yields_interrupted(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    from tests.codex_fakes import agent_message, thread_started, turn_completed
    # Turn 1 ends WITHOUT complete_step (sets need_nudge in interactive).
    # Turn 2 is a sentinel: Guard 2 must prevent it from ever being started.
    scripted = [
        [thread_started("t"), agent_message("a question?"),
         turn_completed(output_tokens=1)],
        [agent_message("SECOND TURN SHOULD NOT RUN"),
         turn_completed(output_tokens=1)],
    ]

    fake_client = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake_client)
    runner._canonical_auth_override = auth

    def _cont(_last_text: str) -> tuple[str, bool]:
        runner._cancel = True   # interrupt lands while the nudge is computed
        return "keep going", False

    inv = _invocation(tmp_path, event_log=elog, continuation_fn=_cont)
    events = [ev async for ev in runner.run(inv)]

    # Guard 2 (post-cont() cancel check) must prevent the second turn from running:
    # only 1 call to run_streamed should have occurred.
    assert fake_client.thread is not None
    assert len(fake_client.thread.run_calls) == 1, (
        f"Guard 2 failed: run_streamed was called {len(fake_client.thread.run_calls)} "
        f"times (expected 1); events={events}")
    assert events[-1] == {"type": "error", "payload": {
        "category": "config", "kind": "interrupted",
        "message": "runner cancelled by interrupt"}}


@pytest.mark.anyio
async def test_cancel_raised_inside_continuation_yields_interrupted(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    from tests.codex_fakes import agent_message, thread_started, turn_completed
    scripted = [[thread_started("t"), agent_message("q?"), turn_completed(output_tokens=1)]]

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = auth

    def _cont(_last_text: str) -> tuple[str, bool]:
        runner._cancel = True
        raise RuntimeError("transport died mid-nudge")

    inv = _invocation(tmp_path, event_log=elog, continuation_fn=_cont)
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["payload"]["kind"] == "interrupted"





@pytest.mark.anyio
async def test_fatal_tool_error_yields_terminal_event_and_bails(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    from tests.codex_fakes import agent_message, thread_started, turn_completed
    scripted = [[thread_started("t"), agent_message("calling lint"),
                 turn_completed(output_tokens=1)],
                [agent_message("should never run"), turn_completed()]]

    fake_client = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake_client)
    runner._canonical_auth_override = auth

    import imp.providers.codex_runner as cr
    orig = cr.process_event
    def _patched(*a: Any, **k: Any) -> None:
        rs = k.get("run_state")
        if rs is not None and getattr(a[0], "type", None) == "turn.completed":
            rs.fatal_tool_error = ("lint", "config missing")
        return orig(*a, **k)
    cr.process_event = _patched
    try:
        events = [ev async for ev in runner.run(_invocation(tmp_path, event_log=elog))]
    finally:
        cr.process_event = orig

    fatal = [e for e in events if e.get("payload", {}).get("kind") == "fatal_tool_error"]
    assert fatal, events
    # Prove the run bailed after turn 1: run_streamed must have been called
    # exactly once (the fatal event fires at turn.completed, so turn 2 must
    # never start).
    assert fake_client.thread is not None
    assert len(fake_client.thread.run_calls) == 1, (
        f"fatal_tool_error bail failed: run_streamed was called "
        f"{len(fake_client.thread.run_calls)} times (expected 1); events={events}")


@pytest.mark.anyio
async def test_killed_tools_drained_to_tool_killed_events(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    from tests.codex_fakes import agent_message, thread_started, turn_completed
    scripted = [[thread_started("t"), agent_message("ran bash"),
                 turn_completed(output_tokens=1)]]

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = auth

    import imp.providers.codex_runner as cr
    orig = cr.process_event
    def _patched(*a: Any, **k: Any) -> None:
        rs = k.get("run_state")
        if rs is not None and getattr(a[0], "type", None) == "item.completed":
            rs.killed_tool_uses.append({"id": "", "name": "bash", "signal": 9})
        return orig(*a, **k)
    cr.process_event = _patched
    try:
        events = [ev async for ev in runner.run(_invocation(tmp_path, event_log=elog))]
    finally:
        cr.process_event = orig

    # tool_killed is an audit event: persisted to the log exactly once
    # (NOT yielded — drive_invocation would otherwise re-append it).
    killed = [e for e in list(elog.read()) if e.get("type") == "tool_killed"]
    assert len(killed) == 1, killed
    assert killed[0]["payload"].get("name") == "bash"
    assert killed[0]["payload"].get("signal") == 9
    # And it must NOT appear in the yielded control-signal stream.
    assert not any(e.get("type") == "tool_killed" for e in events)





_TASK_ARGS = {"subagent_name": "explorer", "task_description": "go look"}


@pytest.mark.anyio
async def test_interrupt_after_sdk_task_start_adds_no_sdk_lifecycle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    from tests.codex_fakes import mcp_call, mcp_started, thread_started, turn_completed
    scripted = [[thread_started("t"),
                 mcp_started("sub_agent", args=_TASK_ARGS, item_id="sub-1"),
                 mcp_call(name="sub_agent", args=_TASK_ARGS,
                          result='{"r":"A"}', item_id="sub-1"),
                 turn_completed(output_tokens=1)]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = auth
    import imp.providers.codex_runner as cr
    original = cr.process_event

    def _cancel_after_start(event: Any, **kwargs: Any) -> None:
        original(event, **kwargs)
        if getattr(event, "type", None) == "item.started":
            runner._cancel = True

    monkeypatch.setattr(cr, "process_event", _cancel_after_start)
    [ev async for ev in runner.run(_invocation(tmp_path, event_log=elog))]

    assert not [e for e in elog.read()
                if e["type"] in ("tool_call", "tool_result")]


@pytest.mark.anyio
async def test_awaiting_user_resume_injects_reply_not_system_prompt(
        tmp_path: Path) -> None:
    """A paused codex wait_for_user (journaled awaiting_user_input, no
    complete_step) derives AWAITING_USER; the runner solicits via user_input_fn
    and injects the reply as the FIRST resumed prompt, not the system prompt."""
    import json
    auth = _seed_git_and_auth(tmp_path)
    from tests.codex_fakes import seed_codex_resume_state
    thread_id = "thr-awaiting-user-1"
    native_resume_state = seed_codex_resume_state(
        tmp_path / "tx",
        thread_id=thread_id,
        restore_path="sessions/awaiting-user.jsonl",
    )
    # Pre-seed events.jsonl: a tail agent_message (agent asked a question) plus the
    # awaiting_user_input event the wait_for_user tool journals before pausing.
    (tmp_path / "events.jsonl").write_text(
        json.dumps({
            "agent_id": "step:t", "type": "agent_message",
            "payload": {"raw": {"content": [
                {"type": "text", "text": "which file?"}]}}}) + "\n" +
        json.dumps({
            "agent_id": "step:t", "type": "awaiting_user_input",
            "payload": {"prompt": "which file?"}}) + "\n")
    elog = InMemoryEventLog()
    from tests.codex_fakes import agent_message, mcp_call, thread_started, turn_completed
    scripted = [[thread_started("t"), agent_message("ok"),
                 mcp_call(name="complete_step", args={"answer": "done"},
                          result="step complete", item_id="cs"),
                 turn_completed(output_tokens=1)]]

    captured = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(model_name="gpt-5.5",
                              client_factory=lambda options=None: captured)
    runner._canonical_auth_override = auth

    def _solicit(prompt: str) -> str:
        # user_input_fn is solicited with "" (the stored question is re-surfaced
        # via the awaiting_user_input event, not passed here).
        assert prompt == ""
        return "the README"

    inv = _invocation(tmp_path, event_log=elog,
                      user_input_fn=_solicit)
    # Mark this as a resume.
    object.__setattr__(inv, "balance_on_resume", True)
    object.__setattr__(inv, "native_resume_state", native_resume_state)
    [ev async for ev in runner.run(inv)]

    # The first resumed turn's prompt is the user's reply, not the system prompt.
    first_prompt = captured.thread.run_calls[0][0]
    assert "the README" in str(first_prompt)
    assert "you are a test agent" not in str(first_prompt)



