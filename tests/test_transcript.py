# tests/test_transcript.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import (
    COMPLETE_STEP_BATCHED,
    RESUME_DURABILITY_NOTE,
    RESUME_REDO_NUDGE,
    REWORK_RESULT_FRAME,
    ContinuationNote,
    SkillReply,
    Transcript,
    answer_complete_step_on_resume,
    balance_success_on_resume,
    complete_step_tail,
    deliver_continuation_reply,
    deliver_engine_continuation,
    deliver_reply_on_resume,
    repair_dangling_tool_use_on_resume,
    repair_interrupt_marker_on_resume,
    unresolved_tool_result_content,
)
from imp.engine.state import InMemoryEventLog


def test_load_empty_returns_empty(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    assert t.load() == []


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    m1 = Message(role="user", content=(TextBlock(text="hi"),))
    m2 = Message(
        role="assistant",
        content=(
            TextBlock(text="reading..."),
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


def test_inject_after_complete_step_delivers_feedback_as_error_result(
    tmp_path: Path,
) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    # Targeted-step shape: assistant ended with a SUCCESSFUL complete_step.
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant", content=(
            TextBlock(text="done"),
            ToolUseBlock(id="c1", name="complete_step", input={"status": "Success"}),
        )),
    ]
    for m in msgs:
        t.append(m)
    elog = InMemoryEventLog()

    injected = balance_success_on_resume(msgs, t, elog, "step:p/s", wrapper_text="the test failed")

    # Exactly one new trailing user message.
    assert msgs[-1] is injected
    assert injected.role == "user"
    # The feedback IS the complete_step result: is_error-shaped and framed.
    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    texts = [b for b in injected.content if isinstance(b, TextBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "c1"
    assert results[0].is_error is True
    assert results[0].content == REWORK_RESULT_FRAME + "\n\n" + "the test failed"
    # No separate feedback text block — only the resume-durability note rides along.
    assert [b.text for b in texts] == [RESUME_DURABILITY_NOTE]
    # classify_resume now routes onto the live SEND_TO_LLM turn.
    assert classify_resume(msgs) is ResumeState.SEND_TO_LLM
    # Persisted to disk and audit-logged once.
    reloaded = Transcript(tmp_path / "transcript.jsonl").load()
    assert classify_resume(reloaded) is ResumeState.SEND_TO_LLM
    assert [e["type"] for e in elog.read()] == ["engine_message"]


def test_inject_after_genuine_interrupt_uses_error_result(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="b1", name="bash", input={"cmd": "x"}),
        )),
    ]
    for m in msgs:
        t.append(m)
    elog = InMemoryEventLog()

    injected = balance_success_on_resume(msgs, t, elog, "step:p/s", wrapper_text="feedback")

    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "b1"
    assert results[0].is_error is True          # genuine interrupt -> error-flavored
    assert classify_resume(msgs) is ResumeState.SEND_TO_LLM


def _completed(tmp_path: Path) -> tuple[list[Message], Transcript]:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant",
                content=(ToolUseBlock(
                    id="tu1", name="complete_step", input={"status": "Success"}),)),
    ]
    for m in msgs:
        t.append(m)
    return msgs, t


def test_balance_without_wrapper_emits_success_shaped_result(tmp_path: Path) -> None:
    msgs, t = _completed(tmp_path)
    elog = InMemoryEventLog()
    out = balance_success_on_resume(msgs, t, elog, "step:x", wrapper_text=None)
    assert out is not None
    result = msgs[-1]
    assert result.role == "user"
    blocks = [b for b in result.content if isinstance(b, ToolResultBlock)]
    assert blocks and blocks[0].is_error is False         # success-shaped, not "interrupted"
    # Even with no wrapper, the resume-durability note rides on the turn — and it
    # is the only text block (no feedback to follow it).
    texts = [b for b in result.content if isinstance(b, TextBlock)]
    assert [b.text for b in texts] == [RESUME_DURABILITY_NOTE]


def test_balance_with_wrapper_rides_on_same_user_turn(tmp_path: Path) -> None:
    msgs, t = _completed(tmp_path)
    elog = InMemoryEventLog()
    balance_success_on_resume(msgs, t, elog, "step:x", wrapper_text="not accepted")
    texts = [b for b in msgs[-1].content if isinstance(b, TextBlock)]
    results = [b for b in msgs[-1].content if isinstance(b, ToolResultBlock)]
    # Rework feedback rides IN the is_error complete_step result, framed.
    assert results[0].is_error is True
    assert results[0].content == REWORK_RESULT_FRAME + "\n\n" + "not accepted"
    # Only the durability note remains as a text block (no feedback text block).
    assert [b.text for b in texts] == [RESUME_DURABILITY_NOTE]


def test_balance_noop_when_no_trailing_tool_use(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [Message(role="user", content=(TextBlock(text="hi"),))]
    t.append(msgs[0])
    assert balance_success_on_resume(msgs, t, InMemoryEventLog(), "x", wrapper_text=None) is None


def test_balance_fallback_delivers_wrapper_as_text_block_when_no_trailing_tool_use(
    tmp_path: Path,
) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant", content=(TextBlock(text="thinking out loud"),)),
    ]
    for m in msgs:
        t.append(m)
    elog = InMemoryEventLog()

    injected = balance_success_on_resume(msgs, t, elog, "step:p/s", wrapper_text="review failed")

    # A new trailing user message IS returned (not None).
    assert injected is not None
    assert msgs[-1] is injected
    assert injected.role == "user"
    # No ToolResultBlock — there was nothing to balance.
    assert not any(isinstance(b, ToolResultBlock) for b in injected.content)
    # Wrapper feedback rides as a trailing TextBlock (the fallback path).
    texts = [b for b in injected.content if isinstance(b, TextBlock)]
    assert any(b.text == "review failed" for b in texts)
    # Resume-durability note is also present.
    assert any(b.text == RESUME_DURABILITY_NOTE for b in texts)
    # A live user turn was appended — routes to SEND_TO_LLM.
    assert classify_resume(msgs) is ResumeState.SEND_TO_LLM


def test_balance_success_on_resume_delivers_feedback_as_error_result(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant", content=(
            TextBlock(text="done"),
            ToolUseBlock(id="c9", name="complete_step", input={"status": "Success"}),
        )),
    ]
    for m in msgs:
        t.append(m)
    injected = balance_success_on_resume(msgs, t, InMemoryEventLog(), "step:x",
                                         wrapper_text="feedback")
    assert injected is not None
    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].content == REWORK_RESULT_FRAME + "\n\n" + "feedback"


def test_repair_dangling_tool_use_appends_durability_note(tmp_path: Path) -> None:
    """A genuine mid-tool interrupt synthesizes is_error placeholders; the
    resume-durability note rides on the same turn so the model verifies rather
    than redoes the interrupted work."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="b1", name="bash", input={"command": "x"}),)),
    ]
    for m in msgs:
        t.append(m)
    synth = repair_dangling_tool_use_on_resume(msgs, t)
    assert synth is not None
    assert synth and synth[0].is_error is True
    texts = [b for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert [b.text for b in texts] == [RESUME_DURABILITY_NOTE]
    # The note must survive the round-trip to disk (replayed verbatim on resume).
    assert any(RESUME_DURABILITY_NOTE in b.text
               for b in t.load()[-1].content if isinstance(b, TextBlock))


def test_repair_dangling_sub_agent_tool_use_resumes_cleanly(tmp_path: Path) -> None:
    """Backward-compat: a quest paused mid-subagent under the PRE-CHANGE code left
    the parent transcript with a dangling `sub_agent` tool_use (the old
    SubagentSuspended unwound before any tool_result was committed). Under the new
    code the existing resume-repair synthesizes a placeholder is_error tool_result,
    balancing the transcript so the parent resumes without error — no migration."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="s1", name="sub_agent",
                         input={"subagent_name": "worker",
                                "task_description": "do x"}),)),
    ]
    for m in msgs:
        t.append(m)

    synth = repair_dangling_tool_use_on_resume(msgs, t)

    assert synth is not None
    assert [b.tool_use_id for b in synth] == ["s1"]
    assert synth[0].is_error is True
    assert "[interrupted]" in synth[0].content
    texts = [b.text for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert texts == [RESUME_DURABILITY_NOTE]
    # Every tool_use now has a matching tool_result, so the reloaded transcript is
    # well-formed and resumable (SEND_TO_LLM), not a 400-inducing dangling turn.
    reloaded = Transcript(tmp_path / "transcript.jsonl").load()
    assert classify_resume(reloaded) is ResumeState.SEND_TO_LLM


def test_repair_balances_trailing_interrupt_user_turn(tmp_path: Path) -> None:
    """'…, assistant(tool_use X), user("[Request interrupted by user]")' — the
    repair injects the synthetic tool_result INTO that trailing user turn (not a
    new appended message), so every tool_use is matched in the immediately
    following user turn and the replayed history is well-formed."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="x1", name="bash", input={"command": "x"}),)),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
    ]
    for m in msgs:
        t.append(m)

    synth = repair_dangling_tool_use_on_resume(msgs, t)

    assert synth is not None
    assert len(msgs) == 3                  # edited in place, no new trailing turn
    assert [b.tool_use_id for b in synth] == ["x1"]
    assert synth[0].is_error is True
    # The original interrupt text is preserved on the same (in-place) turn.
    assert any(isinstance(b, TextBlock) and "[Request interrupted by user]" in b.text
               for b in msgs[-1].content)
    # The balance survives the round-trip to disk.
    reloaded = Transcript(tmp_path / "transcript.jsonl").load()
    assert classify_resume(reloaded) is ResumeState.SEND_TO_LLM
    reloaded_results = [
        b for m in reloaded for b in m.content if isinstance(b, ToolResultBlock)]
    assert [r.tool_use_id for r in reloaded_results] == ["x1"]


def test_repair_balances_multiple_unmatched_tool_use_in_trailing_user_turn(tmp_path: Path) -> None:
    """F4 shape-2: two unmatched tool_use blocks in the last assistant turn, followed
    by a trailing user interrupt — both are patched into that user turn, no new turn
    appended."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="a1", name="bash", input={"command": "x"}),
            ToolUseBlock(id="a2", name="bash", input={"command": "y"}),
        )),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
    ]
    for m in msgs:
        t.append(m)

    synth = repair_dangling_tool_use_on_resume(msgs, t)

    assert synth is not None
    assert len(msgs) == 3
    tool_use_ids = {r.tool_use_id for r in synth}
    assert tool_use_ids == {"a1", "a2"}
    assert any(isinstance(b, TextBlock) and "[Request interrupted by user]" in b.text
               for b in msgs[-1].content)


def test_repair_returns_only_synthesized_blocks_on_partial_match(tmp_path: Path) -> None:
    """Shape-2 partial match: the trailing user turn already carries a matched
    tool_result (m1) plus interrupt text, and the preceding assistant turn has a
    second, unmatched tool_use (u1). The returned tuple must contain ONLY the
    newly synthesized block (u1), while the in-place trailing turn carries BOTH
    results — the pre-existing matched one is never re-emitted."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="m1", name="bash", input={"command": "x"}),
            ToolUseBlock(id="u1", name="bash", input={"command": "y"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="m1", content="ok"),
            TextBlock(text="[Request interrupted by user]"),
        )),
    ]
    for m in msgs:
        t.append(m)

    synth = repair_dangling_tool_use_on_resume(msgs, t)

    assert synth is not None
    assert [b.tool_use_id for b in synth] == ["u1"]
    assert all(isinstance(b, ToolResultBlock) for b in synth)
    result_ids = {
        b.tool_use_id for b in msgs[-1].content if isinstance(b, ToolResultBlock)
    }
    assert result_ids == {"m1", "u1"}
    assert classify_resume(t.load()) is ResumeState.SEND_TO_LLM



def test_repair_noop_when_trailing_user_already_balanced(tmp_path: Path) -> None:
    """A trailing user turn that already carries the matching tool_result needs no
    repair (the wait_for_user / complete_step balance paths must be unaffected)."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="assistant",
                content=(ToolUseBlock(id="x1", name="bash", input={"command": "x"}),)),
        Message(role="user",
                content=(ToolResultBlock(tool_use_id="x1", content="ok"),)),
    ]
    for m in msgs:
        t.append(m)
    assert repair_dangling_tool_use_on_resume(msgs, t) is None
    assert len(msgs) == 2


def test_deliver_reply_on_resume_appends_user_text_message(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "t.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="what color?"),)),
    ]
    for m in msgs:
        t.append(m)
    log = InMemoryEventLog()

    injected = deliver_reply_on_resume(msgs, t, log, "step:p/s", "blue", human=True)

    # Delivered as a plain user TextBlock turn — NOT a tool_result fill.
    assert injected.role == "user"
    assert len(injected.content) == 1
    block = injected.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "blue"
    assert msgs[-1] is injected

    # Persisted to disk: the transcript now ends on the user reply.
    reloaded = Transcript(tmp_path / "t.jsonl").load()
    assert reloaded[-1].role == "user"
    assert reloaded[-1].content[0].text == "blue"

    # Journaled as a user_message event.
    assert [e["type"] for e in log.read()] == ["user_message"]


_CS = ToolUseBlock(id="c0", name="mcp__imp__complete_step", input={"status": "Success"})
_REDO = Message(role="user", content=(TextBlock(text=RESUME_REDO_NUDGE),))


def _cs_user(*blocks: TextBlock | ToolResultBlock) -> Message:
    return Message(role="user", content=blocks)


def _cs_history(*tail: Message) -> list[Message]:
    return [_cs_user(TextBlock(text="go")),
            Message(role="assistant", content=(_CS,)), *tail]


def _cs_answer(tmp_path: Path, msgs: list[Message], feedback: str | None,
               ) -> tuple[list[Message], InMemoryEventLog, Transcript]:
    t = Transcript(tmp_path / "transcript.jsonl")
    t.rewrite(msgs)
    elog = InMemoryEventLog()
    cs = complete_step_tail(msgs)
    assert cs == _CS
    answer_complete_step_on_resume(msgs, t, elog, "step:p/s", cs, feedback)
    assert t.load() == msgs
    return msgs, elog, t


def _cs_results(msgs: list[Message]) -> list[ToolResultBlock]:
    return [b for m in msgs for b in m.content
            if isinstance(b, ToolResultBlock) and b.tool_use_id == "c0"]


@pytest.mark.parametrize("tail", [
    (),
    (_cs_user(ToolResultBlock(tool_use_id="c0", content="step complete")),),
    (_cs_user(ToolResultBlock(tool_use_id="c0", content="step complete")),
     _cs_user(TextBlock(text="OLD FEEDBACK"))),
])
def test_complete_step_tail_finds_the_call_under_user_turns(
        tail: tuple[Message, ...]) -> None:
    assert complete_step_tail(_cs_history(*tail)) == _CS


def test_complete_step_tail_none_off_a_complete_step_tail() -> None:
    bash = ToolUseBlock(id="b1", name="bash", input={"cmd": "x"})
    assert complete_step_tail(_cs_history(
        _cs_user(ToolResultBlock(tool_use_id="c0", content="step complete")),
        Message(role="assistant", content=(TextBlock(text="all done"),)))) is None
    assert complete_step_tail([_cs_user(TextBlock(text="go")),
                               Message(role="assistant", content=(bash,))]) is None
    assert complete_step_tail([]) is None
    assert complete_step_tail([_cs_user(TextBlock(text="go"))]) is None
    assert complete_step_tail(
        [Message(role="system", content=(TextBlock(text="sys"),))]) is None


def test_answer_complete_step_rework_replaces_stored_result_in_place(
        tmp_path: Path) -> None:
    fb = "REVISE: fix X"
    stored = ToolResultBlock(tool_use_id="c0", content=[{"type": "text", "text": "step complete"}])
    msgs, elog, _ = _cs_answer(tmp_path, _cs_history(_cs_user(stored)), fb)
    framed = ToolResultBlock(tool_use_id="c0", content=REWORK_RESULT_FRAME + "\n\n" + fb,
                             is_error=True)
    assert msgs[2:] == [_cs_user(framed), _REDO]
    evs = list(elog.read())
    assert [(e["type"], e["payload"]["subtype"]) for e in evs] == [
        ("engine_message", "continuation"), ("engine_message", "continuation")]
    assert evs[0]["payload"]["content"] == [{
        "type": "tool_result", "tool_use_id": "c0",
        "content": REWORK_RESULT_FRAME + "\n\n" + fb, "is_error": True}]
    assert evs[1]["payload"]["content"] == [{"type": "text", "text": RESUME_REDO_NUDGE}]


def test_answer_complete_step_goto_keeps_stored_success(tmp_path: Path) -> None:
    stored = ToolResultBlock(tool_use_id="c0", content=[{"type": "text", "text": "step complete"}])
    msgs, elog, _ = _cs_answer(tmp_path, _cs_history(_cs_user(stored)), None)
    assert msgs[2:] == [_cs_user(stored), _REDO]
    assert [e["payload"]["content"] for e in elog.read()] == [
        [{"type": "text", "text": RESUME_REDO_NUDGE}]]


def test_answer_complete_step_goto_answers_a_dangling_call(tmp_path: Path) -> None:
    msgs, elog, _ = _cs_answer(tmp_path, _cs_history(), None)
    ok = ToolResultBlock(tool_use_id="c0", content="previous step output recorded; resuming")
    assert msgs[2:] == [_cs_user(ok), _REDO]
    assert [e["type"] for e in elog.read()] == ["engine_message", "engine_message"]
    assert not any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
                   for m in msgs for b in m.content)


def test_answer_complete_step_goto_overrides_a_cli_rejection(tmp_path: Path) -> None:
    rejected = ToolResultBlock(tool_use_id="c0", content="rejected", is_error=True)
    msgs, _, _ = _cs_answer(
        tmp_path, _cs_history(_cs_user(rejected), _cs_user(TextBlock(text="hi"))), None)
    ok = ToolResultBlock(tool_use_id="c0", content="previous step output recorded; resuming")
    assert msgs[2:] == [_cs_user(ok), _cs_user(TextBlock(text="hi")), _REDO]


def test_answer_complete_step_prepends_to_the_trailing_run(tmp_path: Path) -> None:
    fb = "REVISE: fix X"
    msgs, elog, _ = _cs_answer(
        tmp_path, _cs_history(_cs_user(TextBlock(text="OLD FEEDBACK"))), fb)
    framed = ToolResultBlock(tool_use_id="c0", content=REWORK_RESULT_FRAME + "\n\n" + fb,
                             is_error=True)
    assert msgs[2:] == [_cs_user(framed, TextBlock(text="OLD FEEDBACK")), _REDO]
    assert [e["payload"]["content"] for e in elog.read()] == [
        [{"type": "tool_result", "tool_use_id": "c0",
          "content": REWORK_RESULT_FRAME + "\n\n" + fb, "is_error": True}],
        [{"type": "text", "text": RESUME_REDO_NUDGE}]]


def test_answer_complete_step_reapplied_plainly_keeps_the_rework(tmp_path: Path) -> None:
    msgs, _, t = _cs_answer(tmp_path, _cs_history(), "REVISE: fix X")
    before = list(msgs)
    elog = InMemoryEventLog()
    answer_complete_step_on_resume(msgs, t, elog, "step:p/s", _CS, None)
    assert msgs == before and list(elog.read()) == []
    assert sum(m == _REDO for m in msgs) == 1


def test_answer_complete_step_rework_replaces_an_earlier_rework(tmp_path: Path) -> None:
    msgs, _, t = _cs_answer(tmp_path, _cs_history(), "F1")
    elog = InMemoryEventLog()
    answer_complete_step_on_resume(msgs, t, elog, "step:p/s", _CS, "F2")
    assert _cs_results(msgs) == [ToolResultBlock(
        tool_use_id="c0", content=REWORK_RESULT_FRAME + "\n\n" + "F2", is_error=True)]
    assert msgs[-1] == _REDO and sum(m == _REDO for m in msgs) == 1
    assert [e["type"] for e in elog.read()] == ["engine_message"]


def test_answer_complete_step_rework_same_feedback_is_a_no_op(tmp_path: Path) -> None:
    msgs, _, t = _cs_answer(tmp_path, _cs_history(), "F1")
    before = list(msgs)
    elog = InMemoryEventLog()
    answer_complete_step_on_resume(msgs, t, elog, "step:p/s", _CS, "F1")
    assert msgs == before and list(elog.read()) == []


def test_deliver_engine_continuation_journals_internal_event_not_user_message(
    tmp_path: Path,
) -> None:
    msgs: list[Message] = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="asking a question"),)),
    ]
    t = Transcript(tmp_path / "transcript.jsonl")
    elog = InMemoryEventLog()

    injected = deliver_engine_continuation(msgs, t, elog, "step:p/s", "CONTINUE NOW")

    # Drives the LLM: appended as the trailing user turn in messages AND transcript.
    assert msgs[-1] is injected
    assert injected.role == "user"
    assert injected.content[0].text == "CONTINUE NOW"
    assert t.load()[-1].content[0].text == "CONTINUE NOW"

    # Journaled as an INTERNAL engine_message event — never a human user_message.
    types = [e["type"] for e in elog.read()]
    assert types == ["engine_message"]
    assert "user_message" not in types
    cont = next(e for e in elog.read() if e["type"] == "engine_message")
    assert cont["payload"] == {
        "role": "user", "subtype": "continuation",
        "content": [{"type": "text", "text": "CONTINUE NOW"}]}


def test_repair_prefers_captured_success_over_synthetic(tmp_path: Path) -> None:
    """A dangling plain-tool whose (name, input) is in `captured` is repaired with the
    REAL success body and is_error=False — not the synthetic 'interrupted' placeholder.
    The transcript block is mcp__imp__-prefixed; the key strips the prefix."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="w1", name="mcp__imp__write_file",
                                      input={"path": "a.py", "content": "X"}),)),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("write_file", {"path": "a.py", "content": "X"})
    synth = repair_dangling_tool_use_on_resume(
        msgs, t, captured={key: ("wrote 42 bytes to a.py", False)})

    assert synth is not None and len(synth) == 1
    assert synth[0].tool_use_id == "w1"
    assert synth[0].is_error is False
    assert synth[0].content == "wrote 42 bytes to a.py"
    # Survives the round-trip to disk (replayed on resume).
    reloaded_results = [
        b for m in Transcript(tmp_path / "transcript.jsonl").load()
        for b in m.content if isinstance(b, ToolResultBlock)]
    assert reloaded_results[0].content == "wrote 42 bytes to a.py"
    assert reloaded_results[0].is_error is False


def test_repair_falls_back_to_synthetic_when_key_absent(tmp_path: Path) -> None:
    """A dangling tool_use whose (name, input) is NOT in `captured` keeps today's
    synthetic is_error result — a genuinely-killed mid-execution tool has nothing
    captured to prefer."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="b1", name="mcp__imp__bash",
                                      input={"command": "x"}),)),
    ]
    for m in msgs:
        t.append(m)

    # A captured entry for a DIFFERENT command — no match for {"command": "x"}.
    other = captured_result_key("bash", {"command": "other"})
    synth = repair_dangling_tool_use_on_resume(msgs, t, captured={other: ("ran", False)})

    assert synth is not None and len(synth) == 1
    assert synth[0].tool_use_id == "b1"
    assert synth[0].is_error is True                     # synthetic fallback
    assert "interrupted" in synth[0].content


def test_repair_mixed_captured_and_synthetic_in_one_turn(tmp_path: Path) -> None:
    """Two dangling tool_uses, one captured and one not: the captured one gets its real
    success, the other the synthetic placeholder."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="w1", name="mcp__imp__write_file",
                         input={"path": "a", "content": "X"}),
            ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "y"}),
        )),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("write_file", {"path": "a", "content": "X"})
    synth = repair_dangling_tool_use_on_resume(msgs, t, captured={key: ("wrote a", False)})

    by_id = {b.tool_use_id: b for b in synth}
    assert by_id["w1"].is_error is False and by_id["w1"].content == "wrote a"
    assert by_id["b1"].is_error is True and "interrupted" in by_id["b1"].content


def test_repair_default_captured_none_unchanged(tmp_path: Path) -> None:
    """Omitting `captured` (the default) reproduces today's synthetic-only repair, note
    included."""
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="b1", name="mcp__imp__bash",
                                      input={"command": "x"}),)),
    ]
    for m in msgs:
        t.append(m)

    synth = repair_dangling_tool_use_on_resume(msgs, t)   # no captured

    assert synth is not None and synth[0].is_error is True
    # Durability note still rides on the turn (Task 3 refines when it drops).
    assert any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
               for b in msgs[-1].content)


def test_repair_shape2_prefers_captured_success(tmp_path: Path) -> None:
    """Shape-2 (trailing user interrupt turn): a captured (name, input) is injected as
    the REAL success into the existing user turn and survives the rewrite to disk."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="w1", name="mcp__imp__write_file",
                                      input={"path": "a", "content": "X"}),)),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("write_file", {"path": "a", "content": "X"})
    synth = repair_dangling_tool_use_on_resume(msgs, t, captured={key: ("wrote a", False)})

    assert synth is not None and len(synth) == 1
    assert synth[0].tool_use_id == "w1"
    assert synth[0].is_error is False
    assert synth[0].content == "wrote a"
    # Injected into the trailing user turn and written back to disk via rewrite.
    reloaded = Transcript(tmp_path / "transcript.jsonl").load()
    results = [b for m in reloaded for b in m.content if isinstance(b, ToolResultBlock)]
    assert results[0].content == "wrote a" and results[0].is_error is False
    # Original interrupt text preserved.
    assert any(isinstance(b, TextBlock) and "[Request interrupted by user]" in b.text
               for m in reloaded for b in m.content)


def test_repair_drops_durability_note_when_all_captured(tmp_path: Path) -> None:
    """Every dangling result came from `captured` (real successes) — the durability
    note is dropped (there is no error-shaped reconstruction to explain)."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="w1", name="mcp__imp__write_file",
                         input={"path": "a", "content": "X"}),
            ToolUseBlock(id="w2", name="mcp__imp__edit_file", input={"path": "b"}),
        )),
    ]
    for m in msgs:
        t.append(m)

    captured = {
        captured_result_key("write_file", {"path": "a", "content": "X"}): ("wrote a", False),
        captured_result_key("edit_file", {"path": "b"}): ("edited b", False),
    }
    repair_dangling_tool_use_on_resume(msgs, t, captured=captured)

    texts = [b.text for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert RESUME_DURABILITY_NOTE not in texts
    # The real results are still present.
    results = [b for b in msgs[-1].content if isinstance(b, ToolResultBlock)]
    assert {b.tool_use_id for b in results} == {"w1", "w2"}
    assert all(b.is_error is False for b in results)


def test_repair_keeps_durability_note_when_any_synthetic(tmp_path: Path) -> None:
    """One captured success + one synthetic fallback: the note is kept (an error-shaped
    reconstruction remains)."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="w1", name="mcp__imp__write_file",
                         input={"path": "a", "content": "X"}),
            ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "y"}),
        )),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("write_file", {"path": "a", "content": "X"})
    repair_dangling_tool_use_on_resume(msgs, t, captured={key: ("wrote a", False)})

    texts = [b.text for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert RESUME_DURABILITY_NOTE in texts


def test_repair_shape2_drops_note_when_all_captured(tmp_path: Path) -> None:
    """Shape-2 (trailing user interrupt turn): a captured-only unmatched tool_use drops
    the trailing durability note too, and preserves the original interrupt text."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="w1", name="mcp__imp__write_file",
                                      input={"path": "a", "content": "X"}),)),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("write_file", {"path": "a", "content": "X"})
    repair_dangling_tool_use_on_resume(msgs, t, captured={key: ("wrote a", False)})

    texts = [b.text for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert RESUME_DURABILITY_NOTE not in texts
    assert any("[Request interrupted by user]" in x for x in texts)


def test_repair_shape2_keeps_note_when_any_synthetic(tmp_path: Path) -> None:
    """Shape-2 with one captured success + one synthetic fallback keeps the note."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="w1", name="mcp__imp__write_file",
                         input={"path": "a", "content": "X"}),
            ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "y"}),
        )),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("write_file", {"path": "a", "content": "X"})
    repair_dangling_tool_use_on_resume(msgs, t, captured={key: ("wrote a", False)})

    texts = [b.text for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert RESUME_DURABILITY_NOTE in texts


def test_repair_keeps_note_when_captured_result_is_error(tmp_path: Path) -> None:
    """A captured entry whose is_error is True is not an all-success repair — the note
    is kept even though every dangling tool_use had a captured entry."""
    from imp.agents.transcript import captured_result_key

    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="b1", name="mcp__imp__bash",
                                      input={"command": "y"}),)),
    ]
    for m in msgs:
        t.append(m)

    key = captured_result_key("bash", {"command": "y"})
    synth = repair_dangling_tool_use_on_resume(
        msgs, t, captured={key: ("boom", True)})

    # The captured error result IS used (real body, is_error True)...
    assert synth is not None and synth[0].content == "boom" and synth[0].is_error is True
    # ...but because it is error-shaped, the durability note is retained.
    texts = [b.text for b in msgs[-1].content if isinstance(b, TextBlock)]
    assert RESUME_DURABILITY_NOTE in texts


class _FakeElog:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def append_user_message(self, *, agent_id, message, subtype):
        self.events.append(
            ("user_message", agent_id, message.content[0].text, subtype))

    def append_engine_message(self, *, agent_id, message, subtype):
        self.events.append(
            ("engine_message", agent_id, message.content[0].text, subtype))
        return {}

    def append(self, *, agent_id, type, payload):
        self.events.append((type, agent_id, payload))


class _FakeTranscript:
    def __init__(self) -> None:
        self.msgs: list = []

    def append(self, m) -> None:
        self.msgs.append(m)


def test_deliver_continuation_reply_plain_appends_and_journals_human() -> None:
    elog = _FakeElog()
    messages: list = []
    tr = _FakeTranscript()
    out = deliver_continuation_reply(
        messages, tr, elog, "step:coder", "hello", human=True)
    assert out == "hello"
    assert elog.events == [("user_message", "step:coder", "hello", "reply")]
    assert [m.content[0].text for m in messages] == ["hello"]
    assert [m.content[0].text for m in tr.msgs] == ["hello"]


def test_deliver_continuation_reply_skill_journals_command_load_body_in_order(
) -> None:
    elog = _FakeElog()
    messages: list = []
    tr = _FakeTranscript()
    reply = SkillReply(
        command="/refactor src/foo.py", name="refactor",
        body="BODY\n\nsrc/foo.py\n")
    out = deliver_continuation_reply(
        messages, tr, elog, "step:coder", reply, human=True)
    # Returns the body (drives the next turn), not the command line.
    assert out == "BODY\n\nsrc/foo.py\n"
    # AC-4 ordering: command user_message (human), then skill_load, then body.
    assert elog.events == [
        ("user_message", "step:coder", "/refactor src/foo.py", "reply"),
        ("skill_load", None, {"name": "refactor", "source": "human"}),
        ("engine_message", "step:coder", "BODY\n\nsrc/foo.py\n", "continuation"),
    ]
    # The command is event-only: only the body is appended to messages/transcript.
    assert [m.content[0].text for m in messages] == ["BODY\n\nsrc/foo.py\n"]
    assert [m.content[0].text for m in tr.msgs] == ["BODY\n\nsrc/foo.py\n"]


def test_deliver_continuation_reply_string_model_skips_append() -> None:
    # codex's string-prompt model passes messages=None, transcript=None:
    # it journals but keeps no message-list transcript here.
    elog = _FakeElog()
    out = deliver_continuation_reply(
        None, None, elog, "step:coder", "hello", human=False)
    assert out == "hello"
    assert elog.events == [("engine_message", "step:coder", "hello", "continuation")]


def test_deliver_continuation_reply_skill_string_model_skips_append() -> None:
    elog = _FakeElog()
    reply = SkillReply(
        command="/refactor src/foo.py", name="refactor",
        body="BODY\n\nsrc/foo.py\n")
    out = deliver_continuation_reply(
        None, None, elog, "step:coder", reply, human=True)
    assert out == "BODY\n\nsrc/foo.py\n"
    assert elog.events == [
        ("user_message", "step:coder", "/refactor src/foo.py", "reply"),
        ("skill_load", None, {"name": "refactor", "source": "human"}),
        ("engine_message", "step:coder", "BODY\n\nsrc/foo.py\n", "continuation"),
    ]


def test_deliver_continuation_reply_note_journals_engine_message_despite_human() -> None:
    # Providers hard-code human=True; a ContinuationNote must still journal as a
    # framework injection, never as the user's words.
    elog = _FakeElog()
    messages: list = []
    tr = _FakeTranscript()
    note = ContinuationNote(text="The user has disabled the sandbox.")
    out = deliver_continuation_reply(
        messages, tr, elog, "step:coder", note, human=True)
    assert out == "The user has disabled the sandbox."
    assert elog.events == [
        ("engine_message", "step:coder",
         "The user has disabled the sandbox.", "continuation"),
    ]
    assert [m.content[0].text for m in messages] == [
        "The user has disabled the sandbox."]
    assert [m.content[0].text for m in tr.msgs] == [
        "The user has disabled the sandbox."]


def test_deliver_continuation_reply_note_string_model_skips_append() -> None:
    # codex's string-prompt model passes messages=None, transcript=None.
    elog = _FakeElog()
    note = ContinuationNote(text="The user has disabled the sandbox.")
    out = deliver_continuation_reply(
        None, None, elog, "step:coder", note, human=True)
    assert out == "The user has disabled the sandbox."
    assert elog.events == [
        ("engine_message", "step:coder",
         "The user has disabled the sandbox.", "continuation"),
    ]


# claude-p's verbatim rejection body, as captured live (spike FINDINGS.md).
_REJECTION_BODY = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what "
    "you are doing and wait for the user to tell you how to proceed."
)
_MARKER = "[Request interrupted by user for tool use]"
# The bare variant claude-p writes when the interrupt lands outside a tool call —
# the reason the helper's pattern is wider than the for-tool-use literal.
_MARKER_BARE = "[Request interrupted by user]"
_SYNTHETIC_NON_BASH = (
    "[interrupted] The run was paused before this call's result was captured — this was "
    "NOT a rejection, denial, or failure. On resume, re-run or re-dispatch this call if "
    "its work is still needed, then continue the step."
)
_SYNTHETIC_BASH = (
    '{"stdout": "", "stderr": "interrupted by a pause before result captured — not a '
    'failure; re-run if needed", "returncode": -2}'
)


def _interrupt_tail(
    tmp_path: Path, *tools: ToolUseBlock,
    results: tuple[ToolResultBlock, ...] | None = None,
    marker: str = _MARKER,
) -> tuple[list[Message], Transcript]:
    """The live-observed claude tail: assistant text, assistant tool_use(s),
    user tool_result(s) carrying claude-p's rejection copy, then the marker turn."""
    if results is None:
        results = tuple(
            ToolResultBlock(tool_use_id=tu.id, content=_REJECTION_BODY, is_error=True)
            for tu in tools
        )
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="assistant", content=(TextBlock(text="Dispatching the coder."),)),
        Message(role="assistant", content=tools),
        Message(role="user", content=results),
        Message(role="user", content=(TextBlock(text=marker),)),
    ]
    for m in msgs:
        t.append(m)
    return msgs, t


@pytest.mark.parametrize("marker", [_MARKER, _MARKER_BARE])
def test_interrupt_marker_repair_drops_marker_and_reframes_rejection(
    tmp_path: Path, marker: str,
) -> None:
    """Criterion 1: the marker turn is gone, claude-p's 'rejected / STOP' body is
    replaced by the interrupted-call framing, the durability note is the turn's last
    block, and the rewritten transcript round-trips to exactly the repaired list.
    Both marker wordings fire — the pattern is wider than the for-tool-use literal."""
    msgs, t = _interrupt_tail(
        tmp_path,
        ToolUseBlock(id="tu1", name="mcp__imp__sub_agent",
                     input={"subagent_name": "coder", "task_description": "task 1"}),
        marker=marker,
    )

    repair_interrupt_marker_on_resume(msgs, t)

    assert len(msgs) == 3
    assert not any(
        isinstance(b, TextBlock) and b.text == marker for m in msgs for b in m.content)
    last = msgs[-1]
    assert last.role == "user"
    results = [b for b in last.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1
    assert results[0].tool_use_id == "tu1"
    assert results[0].is_error is True
    assert results[0].content == _SYNTHETIC_NON_BASH
    assert "The tool use was rejected" not in str(results[0].content)
    assert last.content[-1] == TextBlock(text=RESUME_DURABILITY_NOTE)
    # The repaired tail is still a tool_result-bearing user turn.
    assert classify_resume(msgs) is ResumeState.SEND_TO_LLM
    assert Transcript(tmp_path / "transcript.jsonl").load() == msgs


def test_interrupt_marker_repair_is_idempotent(tmp_path: Path) -> None:
    """Criterion 2: a second resume over an already-repaired transcript changes
    nothing — identical messages, identical bytes, exactly one durability note."""
    path = tmp_path / "transcript.jsonl"
    msgs, t = _interrupt_tail(
        tmp_path, ToolUseBlock(id="tu1", name="mcp__imp__sub_agent", input={"x": 1}))
    repair_interrupt_marker_on_resume(msgs, t)
    after_first = list(msgs)
    bytes_first = path.read_bytes()

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs == after_first
    assert path.read_bytes() == bytes_first
    assert sum(1 for b in msgs[-1].content
               if isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE) == 1


def test_interrupt_marker_repair_uses_bash_body_for_bash(tmp_path: Path) -> None:
    """Criterion 3: an interrupted bash call gets the parseable JSON variant."""
    msgs, t = _interrupt_tail(
        tmp_path, ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make test"}))

    repair_interrupt_marker_on_resume(msgs, t)

    results = [b for b in msgs[-1].content if isinstance(b, ToolResultBlock)]
    assert results[0].content == _SYNTHETIC_BASH
    assert results[0].is_error is True


def test_interrupt_marker_repair_leaves_success_results_untouched(tmp_path: Path) -> None:
    """Criterion 5: a non-error tool_result in the same turn is real and survives
    byte-identical; only the is_error sibling is re-framed."""
    ok = ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"})
    killed = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "x"})
    good = ToolResultBlock(tool_use_id="r1", content="file body", is_error=False)
    bad = ToolResultBlock(tool_use_id="b1", content=_REJECTION_BODY, is_error=True)
    msgs, t = _interrupt_tail(tmp_path, ok, killed, results=(good, bad))

    repair_interrupt_marker_on_resume(msgs, t)

    by_id = {b.tool_use_id: b for b in msgs[-1].content if isinstance(b, ToolResultBlock)}
    assert by_id["r1"] == good
    assert by_id["b1"].content == _SYNTHETIC_BASH


def test_interrupt_marker_repair_ignores_ordinary_text_tail(tmp_path: Path) -> None:
    """Criterion 7: a trailing text-only user turn that is NOT the marker — a human
    interject or a delivered continuation — is left alone, bytes included."""
    path = tmp_path / "transcript.jsonl"
    t = Transcript(path)
    msgs = [
        Message(role="assistant",
                content=(ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "x"}),)),
        Message(role="user",
                content=(ToolResultBlock(tool_use_id="b1", content="boom", is_error=True),)),
        Message(role="user", content=(TextBlock(text="actually, stop and check git status"),)),
    ]
    for m in msgs:
        t.append(m)
    before = list(msgs)
    bytes_before = path.read_bytes()

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs == before
    assert path.read_bytes() == bytes_before


def test_interrupt_marker_dropped_over_an_unanswered_tool_use(tmp_path: Path) -> None:
    """Criterion 6(a): interrupt between turns — the marker sits on an assistant turn
    with a dangling tool_use. The marker goes; the unanswered-tool_use sweep then
    answers b1, so no assistant tail is stranded."""
    path = tmp_path / "transcript.jsonl"
    t = Transcript(path)
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "x"}),)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
    ]
    for m in msgs:
        t.append(m)
    beneath = msgs[:2]

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs == beneath
    assert Transcript(path).load() == beneath


def test_interrupt_marker_left_alone_after_plain_assistant_text(tmp_path: Path) -> None:
    """Criterion 6(b): same, with no dangling tool_use beneath the marker."""
    path = tmp_path / "transcript.jsonl"
    t = Transcript(path)
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="thinking about it"),)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
    ]
    for m in msgs:
        t.append(m)
    before = list(msgs)
    bytes_before = path.read_bytes()

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs == before
    assert path.read_bytes() == bytes_before


def test_interrupt_marker_repair_prefers_captured_result(tmp_path: Path) -> None:
    """Criterion 4: a call that really completed before the interrupt is restored from
    the capture sidecar — real content and its captured is_error — while an unmatched
    sibling keeps the synthetic interrupted body. The transcript block is
    mcp__imp__-prefixed; captured_result_key strips the prefix."""
    from imp.agents.transcript import captured_result_key

    done = ToolUseBlock(id="w1", name="mcp__imp__write_file",
                        input={"path": "a.py", "content": "X"})
    killed = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "x"})
    msgs, t = _interrupt_tail(tmp_path, done, killed)
    captured = {
        captured_result_key("write_file", {"path": "a.py", "content": "X"}):
            ("wrote 42 bytes to a.py", False),
    }

    repair_interrupt_marker_on_resume(msgs, t, captured=captured)

    by_id = {b.tool_use_id: b for b in msgs[-1].content if isinstance(b, ToolResultBlock)}
    assert by_id["w1"].content == "wrote 42 bytes to a.py"
    assert by_id["w1"].is_error is False
    assert by_id["b1"].content == _SYNTHETIC_BASH
    assert by_id["b1"].is_error is True
    # Survives the rewrite — this is what gets replayed to the model on resume.
    reloaded = Transcript(tmp_path / "transcript.jsonl").load()
    assert reloaded == msgs


def test_interrupt_marker_repair_drops_the_note_when_every_result_is_recovered(
    tmp_path: Path,
) -> None:
    """Parity with repair_dangling_tool_use_on_resume's all-success gate: the note's
    "may display as errors even when the original action succeeded" hedge exists to
    frame reconstructed, error-shaped blocks. When every restated result is a success
    imp itself recorded, the hedge only teaches the model to distrust the
    authoritative record."""
    from imp.agents.transcript import captured_result_key

    done1 = ToolUseBlock(id="w1", name="mcp__imp__write_file",
                         input={"path": "a.py", "content": "X"})
    done2 = ToolUseBlock(id="w2", name="mcp__imp__write_file",
                         input={"path": "b.py", "content": "Y"})
    msgs, t = _interrupt_tail(tmp_path, done1, done2)
    captured = {
        captured_result_key("write_file", {"path": "a.py", "content": "X"}):
            ("wrote a.py", False),
        captured_result_key("write_file", {"path": "b.py", "content": "Y"}):
            ("wrote b.py", False),
    }

    repair_interrupt_marker_on_resume(msgs, t, captured=captured)

    by_id = {b.tool_use_id: b for b in msgs[-1].content if isinstance(b, ToolResultBlock)}
    assert by_id["w1"].content == "wrote a.py" and by_id["w1"].is_error is False
    assert by_id["w2"].content == "wrote b.py" and by_id["w2"].is_error is False
    assert not any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
                   for b in msgs[-1].content)
    # Survives the rewrite — this is what gets replayed to the model on resume.
    assert Transcript(tmp_path / "transcript.jsonl").load() == msgs


def test_interrupt_marker_repair_keeps_the_note_when_a_result_is_unrecovered(
    tmp_path: Path,
) -> None:
    """One reconstructed block is enough to earn the hedge: the bash result here is
    the synthetic interrupted body, which really may misrepresent what happened."""
    from imp.agents.transcript import captured_result_key

    done = ToolUseBlock(id="w1", name="mcp__imp__write_file",
                        input={"path": "a.py", "content": "X"})
    killed = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "x"})
    msgs, t = _interrupt_tail(tmp_path, done, killed)
    captured = {
        captured_result_key("write_file", {"path": "a.py", "content": "X"}):
            ("wrote 42 bytes to a.py", False),
    }

    repair_interrupt_marker_on_resume(msgs, t, captured=captured)

    assert msgs[-1].content[-1] == TextBlock(text=RESUME_DURABILITY_NOTE)


def test_interrupt_marker_repair_keeps_the_note_when_a_recovered_result_failed(
    tmp_path: Path,
) -> None:
    """The gate is recovered-SUCCESS, not merely recovered — exactly
    _all_captured_success's rule: a captured failure leaves the turn error-shaped, so
    the framing stays."""
    from imp.agents.transcript import captured_result_key

    done = ToolUseBlock(id="w1", name="mcp__imp__write_file",
                        input={"path": "a.py", "content": "X"})
    msgs, t = _interrupt_tail(tmp_path, done)
    captured = {
        captured_result_key("write_file", {"path": "a.py", "content": "X"}):
            ("PermissionError: denied", True),
    }

    repair_interrupt_marker_on_resume(msgs, t, captured=captured)

    by_id = {b.tool_use_id: b for b in msgs[-1].content if isinstance(b, ToolResultBlock)}
    assert by_id["w1"].is_error is True
    assert msgs[-1].content[-1] == TextBlock(text=RESUME_DURABILITY_NOTE)


def _write(tmp_path: Path, msgs: list[Message]) -> Transcript:
    t = Transcript(tmp_path / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    return t


def _note_count(msgs: list[Message]) -> int:
    return sum(1 for m in msgs for b in m.content
               if isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE)


def test_interrupt_marker_repair_restates_every_per_block_rejection(
    tmp_path: Path,
) -> None:
    """claude writes one Message per block: a parallel-call interrupt is one rejection
    turn per outstanding call, then the marker. Every rejection is restated — the
    first from imp's capture, the second as the bash placeholder resolved by id two
    turns back — and the note lands once, on the last restated turn."""
    from imp.agents.transcript import captured_result_key

    sub = ToolUseBlock(id="s1", name="mcp__imp__sub_agent",
                       input={"subagent_name": "coder", "task_description": "t"})
    bash = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make"})
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(sub,)),
        Message(role="assistant", content=(bash,)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="s1", content=_REJECTION_BODY, is_error=True),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content=_REJECTION_BODY, is_error=True),)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
    ]
    t = _write(tmp_path, msgs)
    captured = {captured_result_key("sub_agent", dict(sub.input)): ("coder done", False)}

    repair_interrupt_marker_on_resume(msgs, t, captured=captured)

    assert msgs[3:] == [
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="s1", content="coder done", is_error=False),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content=_SYNTHETIC_BASH, is_error=True),
            TextBlock(text=RESUME_DURABILITY_NOTE))),
    ]
    assert Transcript(tmp_path / "transcript.jsonl").load() == msgs


def test_interrupt_marker_repair_keeps_user_text_after_the_marker(tmp_path: Path) -> None:
    """A real human turn after the marker survives untouched; the run beneath the
    marker is still repaired."""
    bash = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make"})
    human = Message(role="user", content=(TextBlock(text="sandbox is off"),))
    msgs = [
        Message(role="assistant", content=(bash,)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content=_REJECTION_BODY, is_error=True),)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
        human,
    ]
    t = _write(tmp_path, msgs)

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs[1:] == [
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content=_SYNTHETIC_BASH, is_error=True),
            TextBlock(text=RESUME_DURABILITY_NOTE))),
        human,
    ]


def test_interrupt_marker_repair_drops_every_marker_up_to_the_last(tmp_path: Path) -> None:
    bash = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make"})
    msgs = [
        Message(role="assistant", content=(bash,)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content=_REJECTION_BODY, is_error=True),)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
        Message(role="user", content=(TextBlock(text=_MARKER_BARE),)),
    ]
    t = _write(tmp_path, msgs)

    repair_interrupt_marker_on_resume(msgs, t)

    assert len(msgs) == 2
    assert msgs[1].content == (
        ToolResultBlock(tool_use_id="b1", content=_SYNTHETIC_BASH, is_error=True),
        TextBlock(text=RESUME_DURABILITY_NOTE))


def test_interrupt_marker_repair_adds_no_note_over_a_success(tmp_path: Path) -> None:
    """Nothing beneath the marker was cut off, so the 'cut off by that pause' note
    would be false: the marker goes, the real result stays, no note."""
    ok = ToolResultBlock(tool_use_id="r1", content="file body", is_error=False)
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),)),
        Message(role="user", content=(ok,)),
        Message(role="user", content=(TextBlock(text=_MARKER_BARE),)),
    ]
    t = _write(tmp_path, msgs)

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs[1:] == [Message(role="user", content=(ok,))]
    assert Transcript(tmp_path / "transcript.jsonl").load() == msgs


def test_interrupt_marker_repair_leaves_complete_step_results(tmp_path: Path) -> None:
    """A complete_step result is the complete_step-tail answer's to set: the marker
    goes, but its result is neither restated as 'interrupted' nor framed by the note."""
    rejected = ToolResultBlock(tool_use_id="c1", content=_REJECTION_BODY, is_error=True)
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="c1", name="mcp__imp__complete_step",
                         input={"status": "Success"}),)),
        Message(role="user", content=(rejected,)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
    ]
    t = _write(tmp_path, msgs)

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs[1:] == [Message(role="user", content=(rejected,))]


def test_interrupt_marker_repair_adds_no_second_note(tmp_path: Path) -> None:
    """A run that already holds the durability note is not given another."""
    first = Message(role="user", content=(
        ToolResultBlock(tool_use_id="b1", content=_SYNTHETIC_BASH, is_error=True),
        TextBlock(text=RESUME_DURABILITY_NOTE)))
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make"}),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="b2", name="mcp__imp__bash", input={"command": "test"}),)),
        first,
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b2", content=_REJECTION_BODY, is_error=True),)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
    ]
    t = _write(tmp_path, msgs)

    repair_interrupt_marker_on_resume(msgs, t)

    assert _note_count(msgs) == 1
    assert msgs[2:] == [first, Message(role="user", content=(
        ToolResultBlock(tool_use_id="b2", content=_SYNTHETIC_BASH, is_error=True),))]


def test_interrupt_marker_repair_keeps_an_orphan_error_result(tmp_path: Path) -> None:
    """There is no call to restate it as, so the result stays as written."""
    orphan = ToolResultBlock(tool_use_id="gone", content=_REJECTION_BODY, is_error=True)
    msgs = [
        Message(role="assistant", content=(TextBlock(text="working"),)),
        Message(role="user", content=(orphan,)),
        Message(role="user", content=(TextBlock(text=_MARKER),)),
    ]
    t = _write(tmp_path, msgs)

    repair_interrupt_marker_on_resume(msgs, t)

    assert msgs[1:] == [Message(role="user", content=(orphan,))]
    assert _note_count(msgs) == 0


def test_sweep_answers_parallel_fragments_in_a_new_turn(tmp_path: Path) -> None:
    """Per-block parallel calls: both fragments are unanswered. imp's capture answers
    one, the placeholder the other, in one new user turn with the note; each answer
    is journaled as a tool_result event."""
    from imp.agents.transcript import (
        answer_unanswered_tool_uses_on_resume,
        captured_result_key,
    )

    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make"}),)),
    ]
    t = _write(tmp_path, msgs)
    elog = InMemoryEventLog()

    answer_unanswered_tool_uses_on_resume(
        msgs, t, elog, "step:p/s",
        captured={captured_result_key("read_file", {"path": "a.py"}): ("A BODY", False)})

    assert msgs[3:] == [Message(role="user", content=(
        ToolResultBlock(tool_use_id="r1", content="A BODY", is_error=False),
        ToolResultBlock(tool_use_id="b1", content=_SYNTHETIC_BASH, is_error=True),
        TextBlock(text=RESUME_DURABILITY_NOTE)))]
    assert Transcript(tmp_path / "transcript.jsonl").load() == msgs
    events = [e["payload"] for e in elog.read() if e["type"] == "tool_result"]
    assert [(p["id"], p["error"]) for p in events] == [("r1", False), ("b1", True)]


def test_sweep_journals_a_captured_killed_bash_as_killed(tmp_path: Path) -> None:
    from imp.agents.transcript import (
        answer_unanswered_tool_uses_on_resume,
        captured_result_key,
    )

    args = {"command": "sleep 45"}
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="b0", name="mcp__imp__bash", input={"command": "sleep 1"}),
            ToolUseBlock(id="b1", name="mcp__imp__bash", input=args),)),
    ]
    t = _write(tmp_path, msgs)
    elog = InMemoryEventLog()

    answer_unanswered_tool_uses_on_resume(
        msgs, t, elog, "step:p/s",
        captured={captured_result_key("bash", args): (
            '{"returncode": -15, "stdout": ""}', False)})

    events = list(elog.read())
    assert [(e["type"], e["payload"]["id"]) for e in events] == [
        ("tool_result", "b0"), ("tool_result", "b1"), ("tool_killed", "b1")]
    assert events[2]["payload"] == {"id": "b1", "name": "bash", "signal": 15}


def test_sweep_prepends_results_to_the_trailing_user_run(tmp_path: Path) -> None:
    """With user turns already trailing, the answers head the run's first turn and
    the run's later turns are untouched."""
    from imp.agents.transcript import answer_unanswered_tool_uses_on_resume

    r2 = ToolResultBlock(tool_use_id="r2", content="B BODY", is_error=False)
    human = Message(role="user", content=(TextBlock(text="use c.py"),))
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="r2", name="mcp__imp__read_file", input={"path": "b.py"}),)),
        Message(role="user", content=(r2,)),
        human,
    ]
    t = _write(tmp_path, msgs)

    answer_unanswered_tool_uses_on_resume(msgs, t, InMemoryEventLog(), "step:p/s")

    assert msgs[2:] == [
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="r1", content=_SYNTHETIC_NON_BASH, is_error=True),
            r2, TextBlock(text=RESUME_DURABILITY_NOTE))),
        human,
    ]
    assert Transcript(tmp_path / "transcript.jsonl").load() == msgs


def test_sweep_answers_an_interleaved_earlier_fragment(tmp_path: Path) -> None:
    """A fast result streamed back between fragments leaves the FIRST call unanswered
    in an earlier assistant run; the sweep is global, so it is answered too."""
    from imp.agents.transcript import answer_unanswered_tool_uses_on_resume

    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="r2", name="mcp__imp__read_file", input={"path": "b.py"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="r2", content="B BODY", is_error=False),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="r3", name="mcp__imp__read_file", input={"path": "c.py"}),)),
    ]
    t = _write(tmp_path, msgs)

    answer_unanswered_tool_uses_on_resume(msgs, t, InMemoryEventLog(), "step:p/s")

    assert msgs[4:] == [Message(role="user", content=(
        ToolResultBlock(tool_use_id="r1", content=_SYNTHETIC_NON_BASH, is_error=True),
        ToolResultBlock(tool_use_id="r3", content=_SYNTHETIC_NON_BASH, is_error=True),
        TextBlock(text=RESUME_DURABILITY_NOTE)))]


def test_sweep_adds_no_note_when_every_answer_is_a_captured_success(
    tmp_path: Path,
) -> None:
    from imp.agents.transcript import (
        answer_unanswered_tool_uses_on_resume,
        captured_result_key,
    )

    msgs = [Message(role="assistant", content=(
        ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),))]
    t = _write(tmp_path, msgs)

    answer_unanswered_tool_uses_on_resume(
        msgs, t, InMemoryEventLog(), "step:p/s",
        captured={captured_result_key("read_file", {"path": "a.py"}): ("A BODY", False)})

    assert msgs[1:] == [Message(role="user", content=(
        ToolResultBlock(tool_use_id="r1", content="A BODY", is_error=False),))]


def test_sweep_adds_no_second_note_to_the_trailing_run(tmp_path: Path) -> None:
    from imp.agents.transcript import answer_unanswered_tool_uses_on_resume

    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "make"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content=_SYNTHETIC_BASH, is_error=True),
            TextBlock(text=RESUME_DURABILITY_NOTE))),
    ]
    t = _write(tmp_path, msgs)

    answer_unanswered_tool_uses_on_resume(msgs, t, InMemoryEventLog(), "step:p/s")

    assert msgs[2].content[0] == ToolResultBlock(
        tool_use_id="r1", content=_SYNTHETIC_NON_BASH, is_error=True)
    assert _note_count(msgs) == 1


def test_sweep_is_a_no_op_when_everything_is_answered(tmp_path: Path) -> None:
    from imp.agents.transcript import answer_unanswered_tool_uses_on_resume

    path = tmp_path / "transcript.jsonl"
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="r1", name="mcp__imp__read_file", input={"path": "a.py"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="r1", content="A BODY", is_error=False),)),
    ]
    t = _write(tmp_path, msgs)
    before, bytes_before = list(msgs), path.read_bytes()
    elog = InMemoryEventLog()

    answer_unanswered_tool_uses_on_resume(msgs, t, elog, "step:p/s")

    assert msgs == before and path.read_bytes() == bytes_before
    assert not any(e["type"] == "tool_result" for e in elog.read())


def test_unresolved_tool_result_content_states_uncertainty_without_advising_a_rerun(
) -> None:
    """The expiry body must not reuse the interrupted-placeholder's 're-run or
    re-dispatch this call' advice: the call may still be executing, so re-running it
    could duplicate durable work."""
    body = unresolved_tool_result_content("write_file")
    assert body == (
        "imp interrupted this turn; this call was still running and its outcome "
        "was not recorded. Verify the current state before relying on it.")
    assert "re-run" not in body
    assert "rejected" not in body


def test_unresolved_tool_result_content_keeps_bash_parseable_with_null_returncode(
) -> None:
    """Agents parse bash results, so the sentence rides inside the tool's own JSON
    shape — with a NULL returncode, because on expiry no kill has been established
    (the -2 of _INTERRUPTED_BASH_BODY would assert a SIGINT that never happened)."""
    for name in ("bash", "mcp__imp__bash"):
        assert json.loads(unresolved_tool_result_content(name)) == {
            "stdout": "",
            "stderr": unresolved_tool_result_content("write_file"),
            "returncode": None,
        }


def test_append_fsyncs_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume gate-detector reads the transcript, so its trailing turn must
    survive a machine crash — the same durability the event log already holds."""
    import imp.agents.transcript as transcript_mod

    path = tmp_path / "transcript.jsonl"
    synced: list[Path] = []
    real_fsync = transcript_mod.os.fsync

    def fsync(fd: int) -> None:
        synced.append(Path(f"/proc/self/fd/{fd}").resolve())
        real_fsync(fd)

    monkeypatch.setattr(transcript_mod.os, "fsync", fsync)
    Transcript(path).append(Message(role="user", content=(TextBlock(text="hi"),)))

    assert synced == [path.resolve()]


_BATCH_BASH = ToolUseBlock(id="b1", name="mcp__imp__bash", input={"command": "sleep 30"})
_BATCH_CS = ToolUseBlock(id="c1", name="mcp__imp__complete_step", input={"status": "Success"})
_BATCH_REJECTION = ToolResultBlock("c1", COMPLETE_STEP_BATCHED, is_error=True)


def _batch_turn(role: str, *blocks: object) -> Message:
    return Message(role=role, content=blocks)  # type: ignore[arg-type]


def test_complete_step_batched_copy_is_verbatim() -> None:
    assert COMPLETE_STEP_BATCHED == (
        "complete_step was not run: it must be called on its own, after you have "
        "read the results of your other tool calls. Check those results, then call "
        "complete_step again.")


def test_complete_step_tail_is_none_for_a_rejected_call() -> None:
    split = [_batch_turn("assistant", _BATCH_BASH), _batch_turn("assistant", _BATCH_CS),
             _batch_turn("user", _BATCH_REJECTION)]
    together = [_batch_turn("assistant", _BATCH_BASH, _BATCH_CS),
                _batch_turn("user", _BATCH_REJECTION)]
    assert complete_step_tail(split) is None
    assert complete_step_tail(together) is None


def test_complete_step_tail_is_none_for_a_rejected_call_after_marker_repair(
        tmp_path: Path) -> None:
    filler = "The user doesn't want to proceed with this tool use."
    messages = [
        _batch_turn("assistant", _BATCH_BASH), _batch_turn("assistant", _BATCH_CS),
        _batch_turn("user", _BATCH_REJECTION),
        _batch_turn("user", ToolResultBlock("b1", filler, is_error=True)),
        _batch_turn("user", TextBlock(text="[Request interrupted by user for tool use]")),
    ]
    t = Transcript(tmp_path / "transcript.jsonl")
    for m in messages:
        t.append(m)

    repair_interrupt_marker_on_resume(messages, t)

    assert complete_step_tail(messages) is None
    assert _batch_turn("user", _BATCH_REJECTION) in messages
    restated = next(b for m in messages for b in m.content
                    if isinstance(b, ToolResultBlock) and b.tool_use_id == "b1")
    assert restated.content != filler


@pytest.mark.parametrize("content,is_error", [
    ("step complete", False),
    ("Input validation error: status is required", True),
])
def test_complete_step_tail_keeps_any_other_result(content: str, is_error: bool) -> None:
    messages = [_batch_turn("assistant", _BATCH_CS),
                _batch_turn("user", ToolResultBlock("c1", content, is_error=is_error))]
    assert complete_step_tail(messages) == _BATCH_CS
