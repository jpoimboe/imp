"""Store-backed native-resume round-trips against a real `claude` subprocess.

Gated on IMP_LIVE_PROVIDERS=claude_code + the `claude` CLI on PATH. Each test
seeds a PARTIAL session store (the turns the SDK batcher would have flushed at
the prior `result` boundary) plus the matching transcript.jsonl tail, then drives
ClaudeCodeAgentRunner.run against a live claude (client_factory=None). The
runner reconciles the unflushed tail into the store and native-resumes; the
assertions confirm real claude-p recalls the prior tool outputs with zero
re-execution and no `400 due to tool use concurrency issues`.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
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
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.agents.transcript import Transcript
from imp.engine.completion import completion_instruction
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import claude_code
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.claude_code_session_store import (
    message_to_session_records,
    session_key,
    session_store_for,
)
from tests._live import opted_in
from tests.conftest import _init_repo

_MODEL = "claude-haiku-4-5-20251001"


def _live_inv(tmp_path: Path) -> AgentInvocation:
    """A read_file+complete_step invocation rooted at a real git repo, with a
    terse system prompt that forbids re-reading and demands complete_step."""
    agent_def = AgentDef(
        name="resumer", description="",
        tools=frozenset({"complete_step", "read_file"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    return AgentInvocation(
        agent_def=agent_def,
        provider_name="claude_code", model_name=_MODEL,
        # Carry imp's production completion instruction verbatim (every real
        # invocation appends completion_instruction(schema) — agent_run.py). The
        # hand-rolled "call complete_step" line alone left haiku free to end the
        # turn in prose, which under the bare-turn-end yield contract is a
        # legitimate yield (imp can't tell a question from a forgotten
        # complete_step), so the terse prompt — not the resume plumbing — was the
        # flake source. This makes the prompt faithful to what imp actually sends.
        system_prompt=(
            completion_instruction(agent_def.output_schema)
            + "\n\nYou are resuming a step. The file you needed (a.txt) was "
            "ALREADY read and its contents are in the conversation above — the "
            "earlier 'Read a.txt, then finish' instruction is DONE. Do NOT call "
            "read_file again; re-reading is wrong. Your only remaining action is "
            "to call complete_step with status=Success now."
        ),
        # Ignored on the resume path (the runner uses the seeded transcript); a
        # required field, so a placeholder turn.
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
    )


def _read_file_call_count(log: InMemoryEventLog) -> int:
    return sum(
        1 for ev in log.read()
        if ev["type"] == "tool_call" and ev["payload"].get("name") == "read_file")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.timeout(240)  # live resume turn can exceed the 60s global cap under load
@pytest.mark.anyio
async def test_send_to_llm_store_backed_native_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    inv = _live_inv(tmp_path)
    # Mirror the executor's real resume config: every kept-transcript resume
    # carries balance_on_resume=True. The native-resume gate must engage despite
    # it (this is the exact production path that previously fell to a 400ing
    # stream-json replay).
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    # Transcript ends on a balanced user tool_result → SEND_TO_LLM.
    msgs = [
        Message(role="user", content=(TextBlock(text="Read a.txt, then finish."),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "a.txt"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="ALPHA-CONTENT\n",
                            is_error=False),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # The store holds only what the batcher flushed at the prior result boundary
    # (the first two turns). The trailing tool_result is the unflushed tail; it is
    # a completed-call boundary, so a SEND_TO_LLM native resume FOLDS it into the
    # loaded session (so the model recalls the real result) rather than streaming
    # it as a dangling delta. That leaves an empty delta, so the native empty-delta
    # guard kicks the turn with the continuation nudge — claude-p never generates
    # from a loaded session alone, so an empty delta would deadlock.
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    # Spy on the first-query history stream to prove which path ran: native
    # resume folds the tool_result into the store and streams ONLY the
    # continuation nudge (1 turn); a silent stream-json fallback would replay the
    # full transcript (3 turns). Both would otherwise satisfy the behavioral
    # assertions below.
    streamed: list[list[Message]] = []
    _orig_stream = claude_code._stream_history
    monkeypatch.setattr(
        claude_code, "_stream_history",
        lambda messages: (streamed.append(list(messages)), _orig_stream(messages))[1])

    runner = ClaudeCodeAgentRunner(model_name=_MODEL)
    events = [ev async for ev in runner.run(inv)]

    # Native resume drove a real claude turn to completion (no deadlock, no 400,
    # no abort): the stdin tool_result paired against the --resume session's
    # tool_use and the model generated the next turn.
    assert events[-1]["type"] == "agent_output", events[-1]
    assert not any(e["type"] == "error" for e in events), [
        e for e in events if e["type"] == "error"]
    # Zero re-execution: the folded tool_result was recalled, not redone.
    assert _read_file_call_count(inv.event_log) == 0
    # Native path ran: the first query streamed exactly the continuation-nudge
    # turn (the folded tool_result is NOT in the delta), not the whole transcript
    # (which a replay fallback would send).
    from imp.engine.agent_run import _CONTINUATION_NUDGE
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    assert not any(isinstance(b, ToolResultBlock) for b in streamed[0][0].content)
    assert any(isinstance(b, TextBlock) and b.text == _CONTINUATION_NUDGE
               for b in streamed[0][0].content)


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
# Serialize the 3 crash-prone resume tests onto one worker (under --dist
# loadgroup) to cap the peak concurrent claude-CLI footprint they contribute.
@pytest.mark.xdist_group("live_claude_resume")
@pytest.mark.timeout(240)  # live resume turn can exceed the 60s global cap under load
@pytest.mark.anyio
async def test_dangling_tool_use_store_backed_native_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    (tmp_path / "b.txt").write_text("BRAVO-CONTENT\n")
    inv = _live_inv(tmp_path)
    # Mirror the executor's real resume config: every kept-transcript resume
    # carries balance_on_resume=True. The native-resume gate must engage despite
    # it (this is the exact production path that previously fell to a 400ing
    # stream-json replay).
    inv = replace(inv, balance_on_resume=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    # Tail: a balanced read (t0) then an interrupted, unmatched tool_use (t1)
    # with NO tool_result → DANGLING_TOOL_USE.
    msgs = [
        Message(role="user", content=(TextBlock(text="Read a.txt, then finish."),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "a.txt"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="ALPHA-CONTENT\n",
                            is_error=False),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t1", name="mcp__imp__read_file",
                         input={"path": "b.txt"}),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # The batcher flushed only the first two turns at the prior result boundary;
    # the tool_result and the dangling t1 are the unflushed tail.
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    # Spy on the first-query stream: native resume must STREAM the synthesized
    # placeholder tool_result for the dangling t1 (1 turn) to kick the next turn.
    # An empty delta would deadlock (claude-p never self-starts a loaded session).
    streamed: list[list[Message]] = []
    _orig_stream = claude_code._stream_history
    monkeypatch.setattr(
        claude_code, "_stream_history",
        lambda messages: (streamed.append(list(messages)), _orig_stream(messages))[1])

    runner = ClaudeCodeAgentRunner(model_name=_MODEL)
    events = [ev async for ev in runner.run(inv)]

    # The run reached completion past the dangling call without the legacy
    # transcript_invalid abort (which the store assertion below ties to the
    # native path).
    assert events[-1]["type"] == "agent_output", events[-1]
    # The synthesized tool_result for t1 was streamed as the delta (not an empty
    # query): one trailing user turn carrying a tool_result.
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    assert any(isinstance(b, ToolResultBlock) for b in streamed[0][0].content)
    assert not any(
        e["type"] == "error" and e["payload"].get("kind") == "transcript_invalid"
        for e in events)
    # The earlier a.txt read is intact — claude-p recalled it and did not redo it
    # (any re-read of a.txt or b.txt would register a read_file tool_call here).
    assert _read_file_call_count(inv.event_log) == 0
    # Prove the native-resume-with-reconcile path actually ran (not a repaired
    # stream-json fallback, which also clears the assertions above): reconcile +
    # overwrite persists the unflushed tail (tool_result + dangling t1) onto the 2
    # seeded records before resuming, so the store holds >=3 records; the fallback
    # path never writes the store (its session_store is None), leaving the 2 seeds.
    entries = await store.load(skey)
    assert entries is not None and len(entries) >= 3, entries


def _corrective_live_inv(tmp_path: Path) -> AgentInvocation:
    """A complete_step-tail step resumed with a handback. The system prompt tells
    the agent the prior work is recorded and the only remaining action is to call
    complete_step again — no file reads."""
    agent_def = AgentDef(
        name="resumer", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    return AgentInvocation(
        agent_def=agent_def,
        provider_name="claude_code", model_name=_MODEL,
        system_prompt=(
            completion_instruction(agent_def.output_schema)
            + "\n\nYou are resuming a step you already finished. Your previous "
            "complete_step was recorded. The feedback below is the only remaining "
            "instruction. Acknowledge the feedback in one "
            "sentence, then call complete_step with status=Success now."
        ),
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
    )


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.timeout(240)
@pytest.mark.anyio
async def test_complete_step_handback_native_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    inv = _corrective_live_inv(tmp_path)
    # Production gate/review handback: balance_on_resume + retry_feedback.
    inv = replace(inv, balance_on_resume=True,
                  retry_feedback="REVISE: confirm the work is done, then finish.")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    # Transcript ends on a SUCCESSFUL complete_step tool_use (no result).
    msgs = [
        Message(role="user", content=(TextBlock(text="Read a.txt, then finish."),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="c0", name="complete_step",
                         input={"status": "Success"}),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # The store holds only the user turn the batcher flushed; the complete_step
    # assistant turn is the unflushed tail reconciled before native resume.
    await store.append(skey, message_to_session_records(
        msgs[:1], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    # Spy on the first-query stream: the native path streams ONLY the handback
    # (1 user turn) — the is_error feedback result that pairs the dangling call.
    streamed: list[list[Message]] = []
    _orig_stream = claude_code._stream_history
    monkeypatch.setattr(
        claude_code, "_stream_history",
        lambda messages: (streamed.append(list(messages)), _orig_stream(messages))[1])

    runner = ClaudeCodeAgentRunner(model_name=_MODEL)
    events = [ev async for ev in runner.run(inv)]

    # Native resume drove a real claude turn to completion (no deadlock, no 400).
    assert events[-1]["type"] == "agent_output", events[-1]
    assert not any(e["type"] == "error" for e in events), [
        e for e in events if e["type"] == "error"]
    # The handback is the single streamed delta turn — the is_error complete_step
    # result whose content IS the feedback (the dangling call is left in the loaded
    # session and this result pairs it).
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    results = [b for b in streamed[0][0].content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "c0"
    assert results[0].is_error is True
    assert "REVISE: confirm the work is done, then finish." in results[0].content
    # On a rework resume NO success result is folded: the complete_step is left
    # dangling in the loaded session and the is_error feedback result streams as
    # the delta and pairs it. reconcile + overwrite writes [seed user, dangling
    # complete_step assistant] before load; the live turn the batcher flushes
    # afterward appends past it, so target the stable reconciled index 1.
    entries = await store.load(skey)
    assert entries is not None and len(entries) >= 2, entries
    reconciled = entries[1]
    assert reconciled["message"]["role"] == "assistant"
    assert any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        and b.get("id") == "c0" and b.get("name") == "complete_step"
        for b in reconciled["message"]["content"]), reconciled


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.timeout(240)  # live resume turn can exceed the 60s global cap under load
@pytest.mark.anyio
async def test_step_completed_native_resume_redrives_with_feedback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A STEP_COMPLETED tail (trailing prose over a resolved complete_step) with
    rework feedback native-resumes against real claude-p: the feedback streams as
    the single delta, the model re-drives and calls complete_step again, and no
    awaiting_user_input / runner_warning is emitted."""
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    inv = _corrective_live_inv(tmp_path)
    # Mirror the executor's real kept-transcript resume config.
    inv = replace(
        inv, balance_on_resume=True,
        retry_feedback=(
            "Your recorded outcome needs revision. Call complete_step with "
            "status=Success now to re-record it; do not read any file."))
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    # Incident shape: resolved complete_step masked by one trailing prose turn.
    msgs = [
        Message(role="user", content=(TextBlock(text="Finish the step."),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="cs", name="complete_step",
                         input={"status": "Success"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="cs",
                            content="previous step output recorded",
                            is_error=False),)),
        Message(role="assistant", content=(TextBlock(text="All done — summary."),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # The store flushed only through the complete_step result (msgs[:3]); the
    # trailing prose (msgs[3]) is the unflushed deficit reconcile_store_tail
    # appends before claude-p loads its native session.
    await store.append(skey, message_to_session_records(
        msgs[:3], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    # Spy on the first-query history stream to prove the native path: native
    # resume streams ONLY the delivered feedback turn (1 turn); a stream-json
    # fallback would replay the full transcript.
    streamed: list[list[Message]] = []
    _orig_stream = claude_code._stream_history
    monkeypatch.setattr(
        claude_code, "_stream_history",
        lambda messages: (streamed.append(list(messages)), _orig_stream(messages))[1])

    runner = ClaudeCodeAgentRunner(model_name=_MODEL)
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_output", events[-1]
    assert not any(e["type"] == "error" for e in events), [
        e for e in events if e["type"] == "error"]
    log_types = [ev["type"] for ev in inv.event_log.read()]
    assert "awaiting_user_input" not in log_types
    assert "runner_warning" not in log_types
    # Native single-turn delta: the first query streamed exactly the delivered
    # feedback turn, not the whole transcript a replay fallback would send.
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    block = streamed[0][0].content[0]
    assert isinstance(block, TextBlock)
    assert block.text == inv.retry_feedback


def _must_not_solicit(_prompt: str) -> str:
    raise AssertionError(
        "resume solicited the user — R4 requires the continuation nudge to "
        "continue the agent, not park at a prompt")


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.timeout(240)  # live resume turn can exceed the 60s global cap under load
@pytest.mark.anyio
async def test_midrun_interrupt_resume_continues_agent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R4: a mid-run leave (Ctrl-D) arms resume_should_continue; on a bare-turn-end
    (AWAITING_USER) tail the native resume takes the continuation nudge and drives
    the agent to complete_step — it never solicits / parks at a prompt."""
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    inv = _live_inv(tmp_path)
    # The ctrl-d arming's runtime shape: resume_should_continue=True (unioned by the
    # executor from State.interrupt_pending) + the kept-transcript balance flag, and
    # a REAL solicit channel that must never fire on the continue path.
    inv = replace(
        inv, balance_on_resume=True, resume_should_continue=True,
        user_input_fn=_must_not_solicit)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    skey = session_key(inv.change_context.project_root, "step:p/s")
    store = session_store_for(inv.transcript_dir)

    # Bare-turn-end tail: a.txt was read and its result recorded, then the agent
    # ENDED ITS TURN on prose without complete_step -> classify_resume == AWAITING_USER,
    # the exact "parks at a prompt" shape R4 must instead continue.
    msgs = [
        Message(role="user", content=(TextBlock(text="Read a.txt, then finish."),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file",
                         input={"path": "a.txt"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="ALPHA-CONTENT\n",
                            is_error=False),)),
        Message(role="assistant", content=(
            TextBlock(text="I read a.txt; it says ALPHA-CONTENT."),)),
    ]
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    # The batcher flushed through the tool_result (msgs[:3]); the trailing assistant
    # prose is the unflushed tail reconciled before the native --resume load.
    await store.append(skey, message_to_session_records(
        msgs[:3], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    # Spy on the first-query history stream: the native continue path streams ONLY
    # the continuation-nudge turn (1 turn); a solicit/replay would look different.
    streamed: list[list[Message]] = []
    _orig_stream = claude_code._stream_history
    monkeypatch.setattr(
        claude_code, "_stream_history",
        lambda messages: (streamed.append(list(messages)), _orig_stream(messages))[1])

    runner = ClaudeCodeAgentRunner(model_name=_MODEL, user_input_fn=_must_not_solicit)
    events = [ev async for ev in runner.run(inv)]

    from imp.engine.agent_run import _CONTINUATION_NUDGE
    # Continued to completion — no deadlock, no 400, no abort.
    assert events[-1]["type"] == "agent_output", events[-1]
    assert not any(e["type"] == "error" for e in events), [
        e for e in events if e["type"] == "error"]
    # R4 crux: it did NOT park — no solicit gate was emitted (the _must_not_solicit
    # sentinel would have raised had _resolve_wait_reply reached its solicit branch).
    log_types = [ev["type"] for ev in inv.event_log.read()]
    assert "awaiting_user_input" not in log_types, log_types
    # The continuation nudge drove the resume: the single streamed delta is exactly
    # the nudge user turn (not the whole-transcript replay, not an empty query).
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    assert any(isinstance(b, TextBlock) and b.text == _CONTINUATION_NUDGE
               for b in streamed[0][0].content), streamed[0][0]
    # Zero re-execution: a.txt was recalled from the loaded session, not re-read.
    assert _read_file_call_count(inv.event_log) == 0
