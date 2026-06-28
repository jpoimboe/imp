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
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.claude_code_session_store import (
    message_to_session_records,
    session_key,
    session_store_for,
)
from tests._live import opted_in
from tests.conftest import _init_repo

_MODEL = "claude-sonnet-4-5"


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
        system_prompt=(
            "You are resuming a step. The file you needed was already read and "
            "its contents are in the conversation above. Do NOT read any file "
            "again. Immediately call complete_step with status=Success."
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
@pytest.mark.anyio
async def test_send_to_llm_store_backed_native_resume(tmp_path: Path) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    inv = _live_inv(tmp_path)
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
    # (the first two turns); the trailing tool_result is the unflushed tail the
    # runner must reconcile in before native-resuming.
    await store.append(skey, message_to_session_records(
        msgs[:2], parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model=_MODEL, start_index=0))

    runner = ClaudeCodeAgentRunner(model_name=_MODEL)
    events = [ev async for ev in runner.run(inv)]

    # Native resume drove a real claude turn to completion (no 400, no abort).
    assert events[-1]["type"] == "agent_output", events[-1]
    assert not any(e["type"] == "error" for e in events), [
        e for e in events if e["type"] == "error"]
    # Zero re-execution: the seeded read was recalled, not redone.
    assert _read_file_call_count(inv.event_log) == 0
    # Prove the native-resume-with-reconcile path actually ran (not a silent
    # stream-json fallback, which would also satisfy the assertions above): the
    # seeded store held only the 2 flushed turns; reconcile + overwrite must have
    # persisted the unflushed tool_result tail (3 records) before resuming, and
    # the fallback path never writes the store (its session_store is None).
    entries = await store.load(skey)
    assert entries is not None and len(entries) >= 3, entries


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.anyio
async def test_dangling_tool_use_store_backed_native_resume(tmp_path: Path) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    (tmp_path / "b.txt").write_text("BRAVO-CONTENT\n")
    inv = _live_inv(tmp_path)
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

    runner = ClaudeCodeAgentRunner(model_name=_MODEL)
    events = [ev async for ev in runner.run(inv)]

    # The run reached completion past the dangling call without the legacy
    # transcript_invalid abort (which the store assertion below ties to the
    # native path).
    assert events[-1]["type"] == "agent_output", events[-1]
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
