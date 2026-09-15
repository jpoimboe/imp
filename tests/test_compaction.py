from __future__ import annotations

import json
from pathlib import Path

import anyio

from imp.agents.compaction import ENGINE_NOTE, RESULT_PLACEHOLDER, compact_transcript
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript
from imp.providers.claude import _project_for_cli
from imp.providers.claude_session_store import (
    message_to_session_records,
    session_store_for,
)


def _seed(tdir: Path) -> Transcript:
    t = Transcript(tdir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="do the thing"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="calling tool"),
        ToolUseBlock(id="tu1", name="read_file", input={"path": "a.py"}),
    )))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="tu1", content="FILE BODY LINE 1\nLINE 2"),
    )))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="tu2", name="bash", input={"command": "ls"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="tu2", content="a.py\nb.py", is_error=False),
    )))
    t.append(Message(role="assistant", content=(TextBlock(text="done"),)))
    return t


def test_stubs_every_tool_result_body_and_returns_count(tmp_path: Path) -> None:
    t = _seed(tmp_path)
    n = compact_transcript(tmp_path)
    assert n == 2
    msgs = t.load()
    results = [b for m in msgs for b in m.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 2
    assert all(b.content == RESULT_PLACEHOLDER for b in results)


def test_preserves_calls_text_and_pairing(tmp_path: Path) -> None:
    t = _seed(tmp_path)
    compact_transcript(tmp_path)
    msgs = t.load()
    uses = [b for m in msgs for b in m.content if isinstance(b, ToolUseBlock)]
    texts = [b.text for m in msgs for b in m.content if isinstance(b, TextBlock)]
    results = [b for m in msgs for b in m.content if isinstance(b, ToolResultBlock)]
    assert {u.id for u in uses} == {"tu1", "tu2"}
    assert "do the thing" in texts and "calling tool" in texts and "done" in texts
    assert {u.id for u in uses} == {b.tool_use_id for b in results}


def test_engine_note_inserted_once_first_as_user(tmp_path: Path) -> None:
    t = _seed(tmp_path)
    compact_transcript(tmp_path)
    msgs = t.load()
    assert msgs[0].role == "user"
    assert msgs[0].content == (TextBlock(text=ENGINE_NOTE),)
    note_count = sum(
        1 for m in msgs for b in m.content
        if isinstance(b, TextBlock) and b.text == ENGINE_NOTE)
    assert note_count == 1


def test_result_placeholder_and_note_are_the_pinned_copy() -> None:
    assert RESULT_PLACEHOLDER == "[tool result omitted by compaction]"
    assert ENGINE_NOTE == (
        '[compaction] Earlier tool results were replaced with "[tool result '
        'omitted by compaction]" to reclaim context; the tool calls that produced '
        'them remain. Re-run any tool whose output you still need.')


def test_compaction_is_idempotent(tmp_path: Path) -> None:
    t = _seed(tmp_path)
    assert compact_transcript(tmp_path) == 2
    assert compact_transcript(tmp_path) == 0
    msgs = t.load()
    note_count = sum(
        1 for m in msgs for b in m.content
        if isinstance(b, TextBlock) and b.text == ENGINE_NOTE)
    assert note_count == 1
    results = [b for m in msgs for b in m.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 2 and all(b.content == RESULT_PLACEHOLDER for b in results)


def test_compaction_removes_the_native_store_when_present(tmp_path: Path) -> None:
    # claude_code: the step has a claude_session/ store beside transcript.jsonl.
    _seed(tmp_path)
    store = session_store_for(tmp_path)
    key = {"project_key": "/p", "session_id": "s1"}
    anyio.run(store.append, key, [{"type": "user", "uuid": "x"}])
    assert (tmp_path / "claude_session").exists()

    compact_transcript(tmp_path)

    # Deleted, so the next native resume rebuilds from the slimmed transcript.
    assert not (tmp_path / "claude_session").exists()


def test_compaction_is_a_noop_on_the_store_for_non_native_providers(
    tmp_path: Path,
) -> None:
    # Anthropic/OpenAI: no store dir; compaction still stubs results and must not raise.
    _seed(tmp_path)
    assert not (tmp_path / "claude_session").exists()

    n = compact_transcript(tmp_path)

    assert n == 2
    assert not (tmp_path / "claude_session").exists()


def test_native_store_rebuilds_with_placeholder_bodies_after_compaction(
    tmp_path: Path,
) -> None:
    # AC4 end-to-end: compact deletes stale store; resume rebuilds slim, not original.
    t = _seed(tmp_path)
    store = session_store_for(tmp_path)
    key = {"project_key": "/p", "session_id": "s1"}
    pre = message_to_session_records(
        t.load(), session_id="s1",
        cwd=str(tmp_path), model="m")
    anyio.run(store.overwrite, key, pre)
    assert "FILE BODY LINE 1" in json.dumps(anyio.run(store.load, key))

    compact_transcript(tmp_path)

    on_disk = anyio.run(store.load, key)
    assert on_disk is None   # stale store deleted, so resume rebuilds from scratch
    rebuilt = message_to_session_records(
        _project_for_cli(t.load()), session_id="s1",
        cwd=str(tmp_path), model="m")
    blob = json.dumps(rebuilt)
    assert RESULT_PLACEHOLDER in blob        # live context now carries the stub
    assert "FILE BODY LINE 1" not in blob    # original tool output is gone
    assert "read_file" in blob               # tool calls survive
    assert "do the thing" in blob            # human/assistant prose survives
