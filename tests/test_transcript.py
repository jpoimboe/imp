# tests/test_transcript.py
from __future__ import annotations

from pathlib import Path

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript


def test_load_empty_returns_empty(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    assert t.load() == []


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    m1 = Message(role="user", content=(TextBlock(text="hi"),))
    m2 = Message(
        role="assistant",
        content=(
            TextBlock(text="reading…"),
            ToolUseBlock(id="t1", name="read_file", input={"path": "/p/x"}),
        ),
    )
    m3 = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content="x"),),
    )
    for m in (m1, m2, m3):
        t.append(m)

    loaded = t.load()
    assert loaded == [m1, m2, m3]
    for m in loaded:
        assert isinstance(m.content, tuple)


def test_append_creates_parent_dir(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "transcript.jsonl"
    t = Transcript(deep)
    t.append(Message(role="user", content=(TextBlock(text="x"),)))
    assert deep.exists()
