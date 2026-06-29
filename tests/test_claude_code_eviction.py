# tests/test_claude_code_eviction.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use

PLACEHOLDER = "[old tool result cleared]"
OLD_TS = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
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


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
    )


def _history(name: str = "read_file") -> list[Message]:
    msgs: list[Message] = [Message(role="user", content=(TextBlock(text="go"),))]
    for t in range(6):  # assistant turns 0..5
        msgs.append(Message(role="assistant", content=(
            ToolUseBlock(id=f"u{t}", name=name, input={"path": "x"}),)))
        msgs.append(Message(role="user", content=(
            ToolResultBlock(tool_use_id=f"u{t}", content=f"body-{t}"),)))
    return msgs


async def _setup_native_resume(
    tmp_path: Path, *, timestamp: str, name: str = "read_file",
) -> tuple[AgentInvocation, Any, dict]:
    """Populate transcript + session store from a 6-turn history, seed
    events.jsonl with `timestamp`, and return (inv, store, skey). The trailing
    user(tool_result) turn makes this a SEND_TO_LLM native resume."""
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    msgs = _history(name)
    transcript = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        transcript.append(m)
    store = session_store_for(inv.transcript_dir)
    skey = session_key(inv.change_context.project_root, inv.agent_id)
    records = message_to_session_records(
        msgs, parent_uuid=None, session_id=skey["session_id"],
        cwd=str(tmp_path), model="claude-sonnet-4-5", start_index=0)
    await store.overwrite(skey, records)
    inv.events_path.write_text(
        json.dumps({"type": "llm_response", "timestamp": timestamp}) + "\n")
    return inv, store, skey


def _completing_factory() -> Any:
    scripted = [
        assistant(text("done"),
                  tool_use("c1", "complete_step",
                           {"status": "Success", "note": "ok"})),
        result(),
    ]
    return lambda opts: FakeClaudeSDKClient(opts, scripted_messages=scripted)


def _store_results(entries: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in entries:
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out[b["tool_use_id"]] = b
    return out


@pytest.mark.anyio
async def test_cold_native_resume_evicts_old_compactable_bodies(
    tmp_path: Path,
) -> None:
    inv, store, skey = await _setup_native_resume(tmp_path, timestamp=OLD_TS)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_completing_factory())
    async for _ in runner.run(inv):
        pass

    loaded = await store.load(skey)
    assert loaded is not None
    results = _store_results(loaded)
    # turns 0..5, keep_recent_turns=3 -> cutoff 2 -> evict u0,u1
    assert results["u0"]["content"] == PLACEHOLDER
    assert results["u1"]["content"] == PLACEHOLDER
    assert results["u2"]["content"] == "body-2"
    assert results["u3"]["content"] == "body-3"
    assert results["u4"]["content"] == "body-4"
    assert results["u5"]["content"] == "body-5"
    # structure preserved on a cleared block
    assert results["u0"]["is_error"] is False
    assert results["u0"]["tool_use_id"] == "u0"
    # tool_use blocks untouched
    uses = {b["id"]: b["name"] for e in loaded
            for b in (e.get("message", {}).get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_use"}
    assert uses["u0"] == "read_file"
    # uuid/parentUuid chain intact (no null parent except the head record)
    parents = [e["parentUuid"] for e in loaded]
    assert parents[0] is None
    assert all(p is not None for p in parents[1:])
    # transcript keeps full bodies; placeholder never written there
    transcript_text = (inv.transcript_dir / "transcript.jsonl").read_text()
    assert "body-0" in transcript_text
    assert PLACEHOLDER not in transcript_text
