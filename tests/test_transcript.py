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
    repair_dangling_tool_use_on_resume,
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
    repair = repair_dangling_tool_use_on_resume(msgs, t)
    assert repair is not None
    results = [b for b in repair.content if isinstance(b, ToolResultBlock)]
    texts = [b for b in repair.content if isinstance(b, TextBlock)]
    assert results and results[0].is_error is True
    assert [b.text for b in texts] == [RESUME_DURABILITY_NOTE]
    # The note must survive the round-trip to disk (replayed verbatim on resume).
    assert any(RESUME_DURABILITY_NOTE in b.text
               for b in t.load()[-1].content if isinstance(b, TextBlock))
