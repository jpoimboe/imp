from __future__ import annotations

from pathlib import Path

from imp.agents.compaction import ENGINE_NOTE, RESULT_PLACEHOLDER, compact_transcript
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript


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
