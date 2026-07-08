from __future__ import annotations

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolUseBlock
from imp.agents.transcript import Transcript, repair_dangling_tool_use_on_resume
from imp.engine.replay import parse_message


def test_parse_message_dispatches_by_provider() -> None:
    msg = parse_message("anthropic",
                        {"content": [{"type": "text", "text": "x"}]})
    assert msg == Message(role="assistant", content=(TextBlock(text="x"),))


def test_repair_backfills_every_dangling_tool_use_not_just_the_last(tmp_path) -> None:
    # AC9: a suspended BATCH leaves an assistant message whose [task1, task2, task3]
    # tool_uses are ALL dangling (multiple dangling tool_uses, not a trailing one).
    # repair_dangling_tool_use_on_resume must synthesize a placeholder result for
    # EACH, restoring the tool_use<->tool_result balance for the whole batch.
    assistant = Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="sub_agent", input={}),
        ToolUseBlock(id="t2", name="sub_agent", input={}),
        ToolUseBlock(id="t3", name="sub_agent", input={})))
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        assistant]
    transcript = Transcript(tmp_path / "transcript.jsonl")
    synth = repair_dangling_tool_use_on_resume(messages, transcript)
    assert synth is not None
    repaired_ids = sorted(b.tool_use_id for b in synth)
    assert repaired_ids == ["t1", "t2", "t3"]


def test_stall_redrive_balances_torn_down_turn_no_replay(tmp_path) -> None:
    # D5 / AC7: a torn-down turn (unmatched tool_use tail) must be repaired
    # before re-drive, never replayed unbalanced.
    torn_down = Message(role="assistant", content=(
        TextBlock(text="calling a tool"),
        ToolUseBlock(id="t1", name="bash", input={"command": "sleep 99999"})))
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        torn_down]

    # Pre-repair: replaying this tail unbalanced is the 400-triggering shape.
    assert classify_resume(messages) is ResumeState.DANGLING_TOOL_USE

    transcript = Transcript(tmp_path / "transcript.jsonl")
    synth = repair_dangling_tool_use_on_resume(messages, transcript)

    assert synth is not None
    assert [b.tool_use_id for b in synth] == ["t1"]
    # Post-repair: the dangling tool_use is paired, so the re-drive sends a balanced transcript.
    assert classify_resume(messages) is not ResumeState.DANGLING_TOOL_USE
    assert classify_resume(messages) is ResumeState.SEND_TO_LLM
