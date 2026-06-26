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
