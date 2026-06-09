from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock


def _assistant_waiting():
    return Message(role="assistant", content=(
        TextBlock(text="I need a decision."),
        ToolUseBlock(id="wfu1", name="wait_for_user", input={"prompt": "merge?"}),
    ))


def test_dangling_wait_for_user_is_awaiting_user():
    assert classify_resume([_assistant_waiting()]) is ResumeState.AWAITING_USER


def test_other_dangling_tool_is_dangling():
    m = Message(role="assistant", content=(
        ToolUseBlock(id="b1", name="bash", input={"cmd": "ls"}),
    ))
    assert classify_resume([m]) is ResumeState.DANGLING_TOOL_USE


def test_user_message_sends_to_llm():
    m = Message(role="user", content=(ToolResultBlock(tool_use_id="wfu1",
                content="merge", is_error=False),))
    assert classify_resume([m]) is ResumeState.SEND_TO_LLM


def test_text_only_assistant_is_missing_complete_step():
    m = Message(role="assistant", content=(TextBlock(text="all done?"),))
    assert classify_resume([m]) is ResumeState.MISSING_COMPLETE_STEP
