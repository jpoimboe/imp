# tests/test_transcript.py
from __future__ import annotations

from pathlib import Path

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript, inject_retry_feedback
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

    injected = inject_retry_feedback(msgs, t, elog, "step:p/s", "the test failed")

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
    assert classify_resume(msgs, interactive=False) is ResumeState.SEND_TO_LLM
    # Persisted to disk and audit-logged once.
    assert classify_resume(Transcript(tmp_path / "transcript.jsonl").load(),
                           interactive=False) is ResumeState.SEND_TO_LLM
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

    injected = inject_retry_feedback(msgs, t, elog, "step:p/s", "feedback")

    results = [b for b in injected.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1 and results[0].tool_use_id == "b1"
    assert results[0].is_error is True          # genuine interrupt → error-flavored
    assert classify_resume(msgs, interactive=False) is ResumeState.SEND_TO_LLM
