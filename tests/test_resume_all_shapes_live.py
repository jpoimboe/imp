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
    # (the first two turns). The trailing tool_result is the unflushed tail; a
    # SEND_TO_LLM native resume must STREAM it as the delta (it is the message
    # that kicks the next turn), NOT fold it into the loaded session — claude-p
    # never generates from a loaded session alone, so an empty delta deadlocks.
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    # Spy on the first-query history stream to prove which path ran: native
    # resume streams ONLY the trailing tool_result (1 turn); a silent stream-json
    # fallback would replay the full transcript (3 turns). Both would otherwise
    # satisfy the behavioral assertions below.
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
    # Zero re-execution: the seeded read was recalled, not redone.
    assert _read_file_call_count(inv.event_log) == 0
    # Native path ran: the first query streamed exactly the trailing tool_result
    # turn, not the whole transcript (which a replay fallback would send).
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    assert any(isinstance(b, ToolResultBlock) for b in streamed[0][0].content)


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
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
        system_prompt=(
            completion_instruction(agent_def.output_schema)
            + "\n\nYou are resuming a step you already finished. Your previous "
            "complete_step was recorded. The feedback below is the only remaining "
            "instruction. Do NOT call read_file. Acknowledge the feedback in one "
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
    # (1 user turn). The success result rides the loaded session, not the delta.
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
    # The handback is the single streamed delta turn (the success result is NOT here).
    assert len(streamed) == 1, streamed
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user", streamed[0]
    assert all(isinstance(b, TextBlock) for b in streamed[0][0].content)
    assert "REVISE: confirm the work is done, then finish." in "".join(
        b.text for b in streamed[0][0].content if isinstance(b, TextBlock))
    # Zero re-execution: no read_file was issued on resume.
    assert _read_file_call_count(inv.event_log) == 0
    # The store was balanced before load: reconcile + overwrite wrote the
    # complete_step assistant turn and the synthesized success-result user turn
    # onto the single seed (the replay fallback never writes the store).
    entries = await store.load(skey)
    assert entries is not None and len(entries) >= 3, entries
    # reconcile + overwrite writes exactly [seed user, complete_step assistant,
    # synthesized success-result user] before load; the live turn the batcher
    # flushes afterward appends past it, so target the stable reconciled index 2
    # (the success-result user turn), not the drifting store tail.
    reconciled = entries[2]
    assert reconciled["message"]["role"] == "user"
    assert any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        and b.get("tool_use_id") == "c0" and b.get("is_error") is False
        for b in reconciled["message"]["content"]), reconciled
