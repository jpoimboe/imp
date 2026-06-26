import pytest

from imp.agents.classify import LegacyWaitForUserError, classify_resume
from imp.agents.runner import Message, TextBlock, ToolUseBlock


def test_classify_raises_on_legacy_wfu_tool_use():
    msgs = [Message(role="assistant",
                    content=[ToolUseBlock(id="X", name="wait_for_user", input={})])]
    with pytest.raises(LegacyWaitForUserError):
        classify_resume(msgs)


def test_classify_raises_on_mcp_prefixed_legacy_wfu():
    # Transcripts recorded the tool under its MCP-server name; the guard must
    # match that, not just the bare name.
    msgs = [Message(role="assistant",
                    content=[ToolUseBlock(
                        id="X", name="mcp__imp__wait_for_user", input={})])]
    with pytest.raises(LegacyWaitForUserError):
        classify_resume(msgs)


def test_classify_raises_on_legacy_wfu_buried_under_trailing_text():
    # sija65's real shape: an mcp__imp__wait_for_user call earlier in history,
    # then a tool_result and a trailing bare assistant turn. The trailing text
    # alone classifies AWAITING_USER, so the guard must scan the whole history.
    msgs = [
        Message(role="assistant",
                content=[ToolUseBlock(
                    id="X", name="mcp__imp__wait_for_user", input={})]),
        Message(role="user", content=[TextBlock(text="reply")]),
        Message(role="assistant", content=[TextBlock(text="On question 1: no")]),
    ]
    with pytest.raises(LegacyWaitForUserError):
        classify_resume(msgs)


def test_classify_normal_bare_turn_end_still_awaiting():
    from imp.agents.classify import ResumeState
    msgs = [Message(role="assistant", content=[TextBlock(text="a question?")])]
    assert classify_resume(msgs) is ResumeState.AWAITING_USER
