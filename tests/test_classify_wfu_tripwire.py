import pytest

from imp.agents.classify import LegacyWaitForUserError, classify_resume
from imp.agents.runner import Message, TextBlock, ToolUseBlock


def test_classify_raises_on_legacy_wfu_tool_use():
    msgs = [Message(role="assistant",
                    content=[ToolUseBlock(id="X", name="wait_for_user", input={})])]
    with pytest.raises(LegacyWaitForUserError):
        classify_resume(msgs)


def test_classify_normal_bare_turn_end_still_awaiting():
    from imp.agents.classify import ResumeState
    msgs = [Message(role="assistant", content=[TextBlock(text="a question?")])]
    assert classify_resume(msgs) is ResumeState.AWAITING_USER
