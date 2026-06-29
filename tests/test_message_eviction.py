# tests/test_message_eviction.py
from __future__ import annotations

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.providers.message_eviction import apply_eviction, evicted_result_ids


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


def test_apply_swaps_targeted_body_only() -> None:
    msgs = _convo(2)
    out = apply_eviction(msgs, frozenset({"u0"}))
    bodies = {b.tool_use_id: b.content for m in out for b in m.content
              if isinstance(b, ToolResultBlock)}
    assert bodies == {"u0": "[old tool result cleared]", "u1": "body-1"}
    # Non-targeted messages are returned as the SAME object.
    assert out[1] is msgs[1]
    assert out[4] is msgs[4]


def test_apply_empty_set_returns_same_object() -> None:
    msgs = _convo(2)
    assert apply_eviction(msgs, frozenset()) is msgs


def test_apply_preserves_is_error_flag() -> None:
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="u0", name="bash", input={}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="u0", content="boom", is_error=True),)),
    ]
    out = apply_eviction(msgs, frozenset({"u0"}))
    blk = next(b for m in out for b in m.content
              if isinstance(b, ToolResultBlock))
    assert blk.content == "[old tool result cleared]"
    assert blk.is_error is True


def test_apply_leaves_tool_use_and_text_intact() -> None:
    out = apply_eviction(_convo(2), frozenset({"u0"}))
    uses = {b.id: b.name for m in out for b in m.content
            if isinstance(b, ToolUseBlock)}
    assert uses == {"u0": "read_file", "u1": "read_file"}
    assert out[0].content[0] == TextBlock(text="go")


def test_apply_only_swaps_matched_block_in_mixed_message() -> None:
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="ka", name="read_file", input={}),
            ToolUseBlock(id="kb", name="edit_file", input={}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="ka", content="a-body"),
            ToolResultBlock(tool_use_id="kb", content="b-body"),
        )),
    ]
    out = apply_eviction(msgs, frozenset({"ka"}))
    bodies = {b.tool_use_id: b.content for m in out for b in m.content
              if isinstance(b, ToolResultBlock)}
    assert bodies == {"ka": "[old tool result cleared]", "kb": "b-body"}
