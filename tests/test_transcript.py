# tests/test_transcript.py
from __future__ import annotations

from pathlib import Path

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import (
    RESUME_DURABILITY_NOTE,
    RESUME_SUPERSEDED_NOTE,
    Transcript,
    balance_success_on_resume,
    build_resume_manifest,
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


def test_inject_after_complete_step_yields_single_nonerror_user_turn(
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
    # It carries a NON-error result for the complete_step + the feedback text.
    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    texts = [b for b in injected.content if isinstance(b, TextBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "c1"
    assert results[0].is_error is False
    assert texts[-1].text == "the test failed"
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
    assert results[0].is_error is False
    # Note frames the results first; the actionable wrapper stays last.
    assert [b.text for b in texts] == [RESUME_DURABILITY_NOTE, "not accepted"]


def test_balance_noop_when_no_trailing_tool_use(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    msgs = [Message(role="user", content=(TextBlock(text="hi"),))]
    t.append(msgs[0])
    assert balance_success_on_resume(msgs, t, InMemoryEventLog(), "x", wrapper_text=None) is None


def test_balance_success_on_resume_emits_superseded_note_with_wrapper_text(tmp_path: Path) -> None:
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
    assert len(results) >= 1
    assert results[0].content == RESUME_SUPERSEDED_NOTE


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


def test_build_resume_manifest_names_calls_succeeded_and_echoes_writes() -> None:
    messages = [
        Message(role="assistant", content=(
            ToolUseBlock(id="t1", name="mcp__imp__read_file",
                         input={"path": "spec.md"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t1", content="SPEC", is_error=True),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t2", name="mcp__imp__write_file",
                         input={"path": "plan.md", "content": "PLANBODY"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t2", content="ok", is_error=True),)),
    ]
    out = build_resume_manifest(messages)
    assert out is not None
    assert "read_file — succeeded" in out
    assert "write_file — succeeded" in out
    assert "plan.md" in out
    assert "PLANBODY" in out
    assert "replay artifact" in out


def test_build_resume_manifest_none_without_tool_calls() -> None:
    messages = [Message(role="assistant", content=(TextBlock(text="hi"),))]
    assert build_resume_manifest(messages) is None


def test_build_resume_manifest_skips_control_tools() -> None:
    messages = [Message(role="assistant", content=(
        ToolUseBlock(id="c", name="mcp__imp__complete_step", input={}),))]
    assert build_resume_manifest(messages) is None


def test_build_resume_manifest_excludes_named_dangling_ids() -> None:
    messages = [
        Message(role="assistant", content=(
            ToolUseBlock(id="t1", name="mcp__imp__read_file",
                         input={"path": "a.md"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t1", content="A", is_error=True),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t2", name="mcp__imp__read_file",
                         input={"path": "b.md"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t2", content="B", is_error=True),)),
    ]
    # Both calls without exclusion → both named succeeded.
    assert build_resume_manifest(messages).count("read_file — succeeded") == 2
    # Excluding t2 (the genuinely-dangling one) drops exactly its line.
    out = build_resume_manifest(messages, exclude_tool_use_ids=frozenset({"t2"}))
    assert out is not None
    assert out.count("read_file — succeeded") == 1


def test_build_resume_manifest_echoes_edit_file_new_body() -> None:
    messages = [
        Message(role="assistant", content=(
            ToolUseBlock(id="e1", name="mcp__imp__edit_file",
                         input={"path": "x.py", "old": "A", "new": "NEWBODY"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="e1", content="ok", is_error=True),)),
    ]
    out = build_resume_manifest(messages)
    assert out is not None
    assert "edit_file — succeeded" in out
    assert "x.py" in out
    assert "NEWBODY" in out


def test_deliver_reply_on_resume_appends_user_text_message(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "t.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="what color?"),)),
    ]
    for m in msgs:
        t.append(m)
    log = InMemoryEventLog()

    injected = deliver_reply_on_resume(msgs, t, log, "step:p/s", "blue")

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
