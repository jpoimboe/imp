# tests/test_message_eviction.py
from __future__ import annotations

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.providers.message_eviction import evicted_result_ids


def _convo(n_turns: int, name: str = "read_file") -> list[Message]:
    msgs: list[Message] = [Message(role="user", content=(TextBlock(text="go"),))]
    for t in range(n_turns):
        msgs.append(Message(role="assistant", content=(
            ToolUseBlock(id=f"u{t}", name=name, input={}),)))
        msgs.append(Message(role="user", content=(
            ToolResultBlock(tool_use_id=f"u{t}", content=f"body-{t}"),)))
    return msgs


def test_warm_returns_empty_set() -> None:
    assert evicted_result_ids(_convo(6), cold=False) == frozenset()


def test_empty_history_returns_empty_set() -> None:
    assert evicted_result_ids([], cold=True) == frozenset()


def test_cold_evicts_old_compactable_turns() -> None:
    # assistant turns 0..5, keep_recent_turns default 3 -> cutoff 2 -> evict 0,1
    assert evicted_result_ids(_convo(6), cold=True) == frozenset({"u0", "u1"})


def test_recent_turns_excluded_from_set() -> None:
    got = evicted_result_ids(_convo(6), cold=True)
    assert "u2" not in got and "u5" not in got


@pytest.mark.parametrize("name", ["edit_file", "sub_agent", "write_file"])
def test_non_compactable_tool_never_in_set(name: str) -> None:
    assert evicted_result_ids(_convo(6, name), cold=True) == frozenset()


def test_keep_recent_turns_override_widens_eviction() -> None:
    # keep_recent_turns=1 -> cutoff 4 -> evict turns 0,1,2,3
    got = evicted_result_ids(_convo(6), cold=True, keep_recent_turns=1)
    assert got == frozenset({"u0", "u1", "u2", "u3"})


def test_parallel_tool_calls_in_one_assistant_turn() -> None:
    # Two compactable calls in a single assistant message (turn 0), then four
    # later single-call turns (turns 1..4). With keep_recent_turns=3, cutoff is
    # 4-3=1, so only turn 0's two results are evicted.
    msgs: list[Message] = [Message(role="user", content=(TextBlock(text="go"),))]
    msgs.append(Message(role="assistant", content=(
        ToolUseBlock(id="a0", name="read_file", input={}),
        ToolUseBlock(id="b0", name="grep", input={}),
    )))
    msgs.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="a0", content="a-body"),
        ToolResultBlock(tool_use_id="b0", content="b-body"),
    )))
    for t in range(1, 5):
        msgs.append(Message(role="assistant", content=(
            ToolUseBlock(id=f"u{t}", name="read_file", input={}),)))
        msgs.append(Message(role="user", content=(
            ToolResultBlock(tool_use_id=f"u{t}", content=f"body-{t}"),)))
    assert evicted_result_ids(msgs, cold=True) == frozenset({"a0", "b0"})
