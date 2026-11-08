"""Verify _stream_history yields stream-json dicts of the right shape.

The three message types: assistant (with tool_use), user (with
tool_result), and user (with text). Each maps to a specific stream-json
envelope; this test pins the shape so future refactors don't break the
replay path.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.providers.claude import (
    _merge_assistant_fragments,
    _project_for_cli,
    _stream_history,
)
from imp.providers.claude_session_store import message_to_session_records


async def _drain(async_iter: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for x in async_iter:
        out.append(x)
    return out


def test_assistant_with_tool_use_envelope() -> None:
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="reading"),
            ToolUseBlock(id="t1", name="read_file", input={"path": "/p/x"}),
        ),
    )
    out = asyncio.run(_drain(_stream_history([msg])))
    assert len(out) == 1
    env = out[0]
    assert env["type"] == "assistant"
    assert "session_id" in env
    inner = env["message"]
    assert inner["role"] == "assistant"
    assert inner["content"][0] == {"type": "text", "text": "reading"}
    assert inner["content"][1] == {
        "type": "tool_use", "id": "t1", "name": "read_file",
        "input": {"path": "/p/x"},
    }


def test_user_with_tool_result_envelope() -> None:
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content="hello"),),
    )
    out = asyncio.run(_drain(_stream_history([msg])))
    env = out[0]
    assert env["type"] == "user"
    inner = env["message"]
    assert inner["role"] == "user"
    assert inner["content"][0]["type"] == "tool_result"
    assert inner["content"][0]["tool_use_id"] == "t1"


def test_user_text_envelope_inline() -> None:
    """Pure text user messages become inline strings, not block lists."""
    msg = Message(role="user", content=(TextBlock(text="hi"),))
    out = asyncio.run(_drain(_stream_history([msg])))
    inner = out[0]["message"]
    # Either inline string OR a single text block — both are accepted by claude.
    # We chose inline for compactness (the form a new user prompt takes).
    assert inner["content"] == "hi" or inner["content"] == [
        {"type": "text", "text": "hi"},
    ]


def test_merge_collapses_consecutive_user_messages() -> None:
    """An interrupted ToolSearch turn left three consecutive user-role messages
    (tool_result + "[Request interrupted]" + complete_step nudge). The Anthropic
    API requires strict role alternation; replaying three user turns in a row is
    rejected with a 400 ("tool use concurrency issues"). The replay builder must
    fold consecutive same-role messages so the replayed history alternates.

    Mirrors the exact on-disk transcript shape from change 2026-06-06-06x3ml.
    """
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=()),  # empty thinking-only fragment
        Message(role="assistant",
                content=(ToolUseBlock(id="t1", name="ToolSearch", input={}),)),
        Message(role="user",
                content=(ToolResultBlock(tool_use_id="t1", content="schemas"),)),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
        Message(role="user",
                content=(TextBlock(text="You ended your turn without complete_step"),)),
    ]
    out = _merge_assistant_fragments(messages)
    roles = [m.role for m in out]
    assert all(a != b for a, b in itertools.pairwise(roles)), (
        f"replayed history has consecutive same-role messages: {roles}")
    # The three folded user blocks survive in order, tool_result first (the API
    # requires the tool_result to lead the user turn that answers a tool_use).
    last = out[-1]
    assert last.role == "user"
    kinds = [type(b).__name__ for b in last.content]
    assert kinds == ["ToolResultBlock", "TextBlock", "TextBlock"], kinds


def test_merge_still_drops_empty_assistant_and_folds_assistant_fragments() -> None:
    """Existing behavior: empty assistant fragments are dropped and consecutive
    assistant fragments fold into one turn."""
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=()),  # dropped
        Message(role="assistant", content=(TextBlock(text="think"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="t1", name="read_file", input={}),)),
    ]
    out = _merge_assistant_fragments(messages)
    assert [m.role for m in out] == ["user", "assistant"]
    kinds = [type(b).__name__ for b in out[1].content]
    assert kinds == ["TextBlock", "ToolUseBlock"], kinds


def _post_conditions(projected: list[Message]) -> None:
    """The three shape rules claude's loader and the Anthropic API both require."""
    roles = [m.role for m in projected]
    assert all(a != b for a, b in itertools.pairwise(roles)), roles
    assert all(m.content for m in projected), projected
    for m in projected:
        if m.role == "user" and any(isinstance(b, ToolResultBlock) for b in m.content):
            assert isinstance(m.content[0], ToolResultBlock), m


def test_project_pairs_parallel_tool_results_behind_their_tool_use() -> None:
    """Parallel tool_use whose results land in separate later user turns: the CLI's
    loader only pairs the record immediately after the assistant one, so the
    projection must regroup both results into that one turn, in tool_use order."""
    messages = [
        Message(role="user", content=(TextBlock(text="read both"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="read_file", input={"path": "a"}),
            ToolUseBlock(id="t1", name="read_file", input={"path": "b"}),
        )),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t0", content="ALPHA"),)),
        Message(role="assistant", content=(TextBlock(text="one down"),)),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content="BRAVO"),)),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["user", "assistant", "user", "assistant"]
    assert [b.tool_use_id for b in out[2].content] == ["t0", "t1"]
    assert [b.content for b in out[2].content] == ["ALPHA", "BRAVO"]
    # The assistant turn that sat between the two results survives intact.
    assert out[3].content == (TextBlock(text="one down"),)


def test_project_folds_consecutive_user_turns_keeping_result_first() -> None:
    """Regrouping splits a mixed [tool_result, text] user turn; the closing merge
    folds it back, so no two adjacent turns share a role (the documented cause of
    `400 ... tool use concurrency issues`) and the tool_result still leads."""
    messages = [
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="bash", input={"command": "ls"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="a.txt"),
            TextBlock(text="[Request interrupted by user]"),
        )),
        Message(role="user", content=(TextBlock(text="carry on"),)),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["assistant", "user"]
    assert [type(b).__name__ for b in out[1].content] == [
        "ToolResultBlock", "TextBlock", "TextBlock"]
    assert [b.text for b in out[1].content[1:]] == [
        "[Request interrupted by user]", "carry on"]


def test_project_emits_no_empty_message_and_no_empty_record() -> None:
    """A thinking-only (zero-block) assistant fragment is dropped, and a user turn
    whose only blocks were relocated into the pairing turn emits nothing — a
    zero-block leftover here is NOT role-adjacent, so the closing merge would keep
    it and it would reach the store as a `content: []` record."""
    messages = [
        Message(role="assistant", content=()),  # thinking-only fragment
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="read_file", input={"path": "a"}),
            ToolUseBlock(id="t1", name="read_file", input={"path": "b"}),
        )),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t0", content="ALPHA"),)),
        Message(role="assistant", content=(TextBlock(text="half way"),)),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content="BRAVO"),)),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["assistant", "user", "assistant"]
    records = message_to_session_records(
        out, session_id="s0", cwd="/tmp/p", model="m0")
    assert all(r["message"]["content"] for r in records), records


def test_project_leaves_a_dangling_tool_use_unanswered() -> None:
    """A tool_use with no recorded result stays unanswered: the projection
    fabricates nothing (`repair_dangling_tool_use_on_resume` owns that repair)."""
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t9", name="read_file", input={"path": "b"}),)),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["user", "assistant"]
    assert not any(isinstance(b, ToolResultBlock) for m in out for b in m.content)


def test_project_preserves_boundary_tool_results_in_place() -> None:
    """A tool_result whose tool_use is absent from the input list is kept in its
    original order, never dropped: the projection is total. Native resume never
    splits a result from its tool_use (`_enter` cuts the body only before trailing
    user-text turns), so this pins projection totality, not a streamed delta."""
    messages = [
        Message(role="user", content=(ToolResultBlock(
            tool_use_id="c0", content="REVISE: confirm the work is done.",
            is_error=True),)),
        Message(role="assistant", content=(TextBlock(text="on it"),)),
        Message(role="user", content=(ToolResultBlock(tool_use_id="c1", content="second"),)),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["user", "assistant", "user"]
    assert [b.tool_use_id for m in out for b in m.content
            if isinstance(b, ToolResultBlock)] == ["c0", "c1"]
    assert out[0].content[0].is_error is True


def test_project_pairs_only_the_answered_tool_use() -> None:
    """Half-answered parallel tool_use: the pairing turn carries the one recorded
    result and nothing is fabricated for the other."""
    messages = [
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="read_file", input={"path": "a"}),
            ToolUseBlock(id="t1", name="read_file", input={"path": "b"}),
        )),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content="BRAVO"),)),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["assistant", "user"]
    assert [b.tool_use_id for b in out[1].content] == ["t1"]


def test_stream_history_regroups_late_tool_results() -> None:
    """The replayed tail goes through the projection, not the bare merge: a result
    that arrives after an intervening assistant turn is regrouped into the pairing
    turn, so the CLI never sees an unanswered tool_use it would fill with
    "[Tool result missing due to internal error]"."""
    messages = [
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="read_file", input={"path": "a"}),
            ToolUseBlock(id="t1", name="read_file", input={"path": "b"}),
        )),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t0", content="ALPHA"),)),
        Message(role="assistant", content=(TextBlock(text="one down"),)),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content="BRAVO"),)),
    ]
    out = asyncio.run(_drain(_stream_history(messages)))
    assert [d["type"] for d in out] == ["assistant", "user", "assistant"]
    assert [b["tool_use_id"] for b in out[1]["message"]["content"]] == ["t0", "t1"]
    assert [b["content"] for b in out[1]["message"]["content"]] == ["ALPHA", "BRAVO"]


def test_stream_history_keeps_a_boundary_result_delta_intact() -> None:
    """`_stream_history` keeps a one-turn delta whose tool_use lies outside it
    instead of emitting nothing. Native resume never streams such a delta (every
    repaired result is in the loaded body); this pins that no tool_result is
    dropped."""
    delta = [Message(role="user", content=(ToolResultBlock(
        tool_use_id="c0", content="REVISE: fix the claim.", is_error=True),))]
    out = asyncio.run(_drain(_stream_history(delta)))
    assert len(out) == 1 and out[0]["type"] == "user"
    block = out[0]["message"]["content"][0]
    assert block["tool_use_id"] == "c0" and block["is_error"] is True


def test_project_keeps_a_boundary_result_alongside_a_paired_one() -> None:
    """A delta mixing a boundary result with an in-delta tool_use/result pair keeps
    both, tool_results leading, with no adjacent same-role turns."""
    messages = [
        Message(role="user", content=(ToolResultBlock(tool_use_id="c0", content="EARLIER"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="bash", input={"command": "ls"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="a.txt"),
            TextBlock(text="carry on"),
        )),
    ]
    out = _project_for_cli(messages)
    _post_conditions(out)
    assert [m.role for m in out] == ["user", "assistant", "user"]
    assert [b.tool_use_id for b in out[0].content] == ["c0"]
    assert [type(b).__name__ for b in out[2].content] == [
        "ToolResultBlock", "TextBlock"]
    assert out[2].content[0].tool_use_id == "t0"
