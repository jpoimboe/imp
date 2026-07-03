# tests/test_classify.py
from __future__ import annotations

from imp.agents.classify import ResumeState, classify_resume, count_tool_uses
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock


def test_empty_is_fresh() -> None:
    assert classify_resume([]) is ResumeState.FRESH


def test_assistant_text_only_is_awaiting_user() -> None:
    # A bare turn-end (assistant text, no complete_step) IS the yield to the
    # human -> AWAITING_USER. The reply is delivered as the next user message.
    msg = Message(role="assistant", content=(TextBlock(text="?"),))
    assert classify_resume([msg]) is ResumeState.AWAITING_USER


def test_assistant_text_after_user_is_awaiting_user() -> None:
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="what color?"),)),
    ]
    assert classify_resume(msgs) is ResumeState.AWAITING_USER


def test_user_text_means_send_to_llm() -> None:
    msg = Message(role="user", content=(TextBlock(text="hi"),))
    assert classify_resume([msg]) is ResumeState.SEND_TO_LLM


def test_user_tool_results_means_send_to_llm() -> None:
    msg = Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content="x"),))
    assert classify_resume([msg]) is ResumeState.SEND_TO_LLM


def test_dangling_tool_use_is_invalid() -> None:
    msg = Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="bash", input={"command": "ls"}),
    ))
    assert classify_resume([msg]) is ResumeState.DANGLING_TOOL_USE


def test_system_message_recurses() -> None:
    sys_msg = Message(role="system", content=(TextBlock(text="(replay)"),))
    user_msg = Message(role="user", content=(TextBlock(text="hi"),))
    assert classify_resume([user_msg, sys_msg]) is ResumeState.SEND_TO_LLM


def test_count_tool_uses_across_assistant_messages() -> None:
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t1", name="bash", input={}),
            ToolUseBlock(id="t2", name="bash", input={}),
        )),
        Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content=""),
                                      ToolResultBlock(tool_use_id="t2", content=""))),
        Message(role="assistant", content=(ToolUseBlock(id="t3", name="bash", input={}),)),
    ]
    assert count_tool_uses(msgs) == 3


def test_wait_for_user_resume_fill_helpers_removed() -> None:
    import imp.agents.classify as classify_mod
    import imp.agents.transcript as transcript_mod

    assert not hasattr(classify_mod, "unresolved_wait_for_user")
    assert "unresolved_wait_for_user" not in classify_mod.__all__
    assert not hasattr(transcript_mod, "resolve_wait_for_user_on_resume")


def test_resolved_complete_step_with_one_trailing_prose_is_step_completed() -> None:
    # The incident shape: complete_step succeeded (its tool_result is present),
    # then one trailing assistant prose turn survived on top of it.
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="cs", name="complete_step", input={"status": "Success"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="cs", content="ok", is_error=False),
        )),
        Message(role="assistant", content=(TextBlock(text="All done — summary."),)),
    ]
    assert classify_resume(msgs) is ResumeState.STEP_COMPLETED


def test_resolved_complete_step_with_two_trailing_prose_is_step_completed() -> None:
    # Two consecutive trailing text-only assistant turns still resolve to the
    # completed step behind them.
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="cs", name="complete_step", input={"status": "Success"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="cs", content="ok", is_error=False),
        )),
        Message(role="assistant", content=(TextBlock(text="First tail."),)),
        Message(role="assistant", content=(TextBlock(text="Second tail."),)),
    ]
    assert classify_resume(msgs) is ResumeState.STEP_COMPLETED


def test_complete_step_then_other_tool_then_prose_is_awaiting_user() -> None:
    # The resolved complete_step is NOT immediately behind the prose: a bash
    # tool cycle sits between them, so this is a genuine mid-step question.
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="cs", name="complete_step", input={"status": "Success"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="cs", content="ok", is_error=False),
        )),
        Message(role="assistant", content=(
            ToolUseBlock(id="b1", name="bash", input={"command": "ls"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="b1", content="files", is_error=False),
        )),
        Message(role="assistant", content=(TextBlock(text="Which one?"),)),
    ]
    assert classify_resume(msgs) is ResumeState.AWAITING_USER


def test_bare_turn_end_without_complete_step_is_awaiting_user() -> None:
    # A real yield to the human: assistant text with no complete_step behind it.
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="what color?"),)),
    ]
    assert classify_resume(msgs) is ResumeState.AWAITING_USER


def test_dangling_complete_step_at_tail_is_dangling_tool_use() -> None:
    # An unresolved complete_step at [-1] (no tool_result) is still DANGLING.
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="cs", name="complete_step", input={"status": "Success"}),
        )),
    ]
    assert classify_resume(msgs) is ResumeState.DANGLING_TOOL_USE


def test_earlier_lap_complete_step_buried_deep_is_awaiting_user() -> None:
    # An earlier lap's completed complete_step lives deep in history, but the
    # current tail is a genuine bare turn-end. The bounded scan must not reach
    # back to the old completion.
    msgs = [
        Message(role="assistant", content=(
            ToolUseBlock(id="cs1", name="complete_step", input={"status": "Success"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="cs1", content="ok", is_error=False),
        )),
        Message(role="user", content=(TextBlock(text="here is lap-2 feedback"),)),
        Message(role="assistant", content=(TextBlock(text="what should I do?"),)),
    ]
    assert classify_resume(msgs) is ResumeState.AWAITING_USER
