# tests/test_transcript.py
from __future__ import annotations

from pathlib import Path

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import (
    RESUME_DURABILITY_NOTE,
    REWORK_RESULT_FRAME,
    Transcript,
    balance_success_on_resume,
    deliver_engine_continuation,
    deliver_reply_on_resume,
    repair_dangling_tool_use_on_resume,
    synthesize_native_success_on_resume,
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
    assert [e["type"] for e in elog.read()] == ["user_message"]


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


def test_synthesize_native_success_appends_result_only_no_notes(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="c0", name="complete_step",
                         input={"status": "Success"}),)),
    ]
    for m in msgs:
        t.append(m)
    elog = InMemoryEventLog()

    injected = synthesize_native_success_on_resume(msgs, t, elog, "step:p/s")

    # Exactly one new trailing user turn carrying a SUCCESS-shaped result.
    assert injected is msgs[-1] and injected.role == "user"
    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    texts = [b for b in injected.content if isinstance(b, TextBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "c0"
    assert results[0].is_error is False
    assert results[0].content == "previous step output recorded; resuming"
    # NO durability note and NO feedback — the handback rides the streamed delta.
    assert texts == []
    # Persisted to disk and audit-logged exactly once as a user_message.
    assert classify_resume(t.load()) is ResumeState.SEND_TO_LLM
    assert [e["type"] for e in elog.read()] == ["user_message"]


def test_synthesize_native_success_error_flavors_non_complete_step(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [Message(role="assistant", content=(
        ToolUseBlock(id="b1", name="bash", input={"cmd": "x"}),))]
    t.append(msgs[0])

    injected = synthesize_native_success_on_resume(msgs, t, InMemoryEventLog(), "x")

    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "b1"
    # A genuine non-complete_step interrupt is error-flavored, not "succeeded".
    assert results[0].is_error is True
    # No durability note / wrapper feedback on the error-flavored path either.
    assert [b for b in injected.content if isinstance(b, TextBlock)] == []


def test_synthesize_native_success_noop_without_trailing_tool_use(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [Message(role="user", content=(TextBlock(text="hi"),))]
    t.append(msgs[0])
    assert synthesize_native_success_on_resume(
        msgs, t, InMemoryEventLog(), "x") is None


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

    # Journaled as an INTERNAL engine_continuation event — never a human user_message.
    types = [e["type"] for e in elog.read()]
    assert "engine_continuation" in types
    assert "user_message" not in types
    cont = next(e for e in elog.read() if e["type"] == "engine_continuation")
    assert cont["payload"] == {"text": "CONTINUE NOW"}


def test_repair_dangling_complete_step_with_feedback_carries_feedback(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="c0", name="complete_step", input={"status": "Success"}),)),
    ]
    for m in msgs:
        t.append(m)
    synth = repair_dangling_tool_use_on_resume(
        msgs, t, feedback="REVISE: rewrite plan.md")
    assert synth is not None and len(synth) == 1
    rb = synth[0]
    assert rb.tool_use_id == "c0"
    # The dangling complete_step is paired with an is_error result whose content
    # IS the framed feedback (not the synthetic "interrupted" filler).
    assert rb.is_error is True
    assert rb.content == REWORK_RESULT_FRAME + "\n\n" + "REVISE: rewrite plan.md"
    # Appended as a trailing user turn (repair shape) with the durability note.
    assert msgs[-1].role == "user"
    assert any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
               for b in msgs[-1].content)
    assert classify_resume(msgs) is ResumeState.SEND_TO_LLM


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
