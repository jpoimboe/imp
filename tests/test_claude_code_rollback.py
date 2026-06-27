"""Classified terminal-error turns are rolled back so retries re-query."""

from __future__ import annotations

from pathlib import Path

from imp.agents.runner import Message, TextBlock
from imp.agents.transcript import Transcript
from imp.providers.claude_code import _roll_back_error_turn


def test_roll_back_removes_trailing_assistant_turn(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    seed = Message(role="user", content=(TextBlock(text="go"),))
    err = Message(
        role="assistant",
        content=(TextBlock(text="Failed to authenticate. API Error: 401"),),
    )
    messages = [seed, err]
    t.append(seed)
    t.append(err)

    _roll_back_error_turn(messages, t)

    # In-memory list ends on the prior user turn.
    assert [m.role for m in messages] == ["user"]
    block = messages[0].content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "go"

    # Persisted transcript is truncated to match.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user"]
    loaded_block = loaded[0].content[0]
    assert isinstance(loaded_block, TextBlock)
    assert loaded_block.text == "go"


def test_roll_back_removes_only_most_recent_assistant_turn(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    user1 = Message(role="user", content=(TextBlock(text="go"),))
    a1 = Message(role="assistant", content=(TextBlock(text="first assistant turn"),))
    user2 = Message(role="user", content=(TextBlock(text="continue"),))
    a2 = Message(role="assistant", content=(TextBlock(text="second assistant turn - error"),))
    messages = [user1, a1, user2, a2]
    for m in messages:
        t.append(m)

    _roll_back_error_turn(messages, t)

    # Only the last assistant turn (A2) is removed; A1 is intact.
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    a1_block = messages[1].content[0]
    assert isinstance(a1_block, TextBlock)
    assert a1_block.text == "first assistant turn"

    # Persisted transcript matches in-memory list.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    loaded_a1_block = loaded[1].content[0]
    assert isinstance(loaded_a1_block, TextBlock)
    assert loaded_a1_block.text == "first assistant turn"
