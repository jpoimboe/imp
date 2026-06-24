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


def test_wait_for_user_with_trailing_interrupt_turn_is_awaiting_user():
    """A [Request interrupted by user] turn persisted after the dangling ask must
    still classify AWAITING_USER — the wait_for_user has no real reply, so a
    SEND_TO_LLM replay would stream it to the API unbalanced (the 400)."""
    interrupt = Message(role="user", content=(
        TextBlock(text="[Request interrupted by user]"),))
    assert classify_resume([_assistant_waiting(), interrupt]) \
        is ResumeState.AWAITING_USER


def test_wait_for_user_with_placeholder_error_result_is_awaiting_user():
    """A synthetic is_error tool_result for the ask does NOT resolve it — only a
    success-shaped (is_error=False) reply does — so it must still classify
    AWAITING_USER. This pins the is_error-keyed definition of 'resolved'; it is a
    defensive case, not a claim that this exact transcript occurs in production."""
    placeholder = Message(role="user", content=(
        ToolResultBlock(tool_use_id="wfu1", content="awaiting user",
                        is_error=True),))
    assert classify_resume([_assistant_waiting(), placeholder]) \
        is ResumeState.AWAITING_USER


def test_unresolved_wait_for_user_locates_second_unresolved_ask():
    """A resolved first ask and an unresolved second ask: the (index, id) contract
    task 2 relies on returns the SECOND ask's position, not the first."""
    from imp.agents.classify import unresolved_wait_for_user
    first_ask = Message(role="assistant", content=(
        TextBlock(text="I need a decision."),
        ToolUseBlock(id="wfu1", name="wait_for_user", input={"prompt": "merge?"}),
    ))
    reply = Message(role="user", content=(
        ToolResultBlock(tool_use_id="wfu1", content="yes", is_error=False),
    ))
    second_ask = Message(role="assistant", content=(
        TextBlock(text="One more question."),
        ToolUseBlock(id="wfu2", name="wait_for_user", input={"prompt": "really?"}),
    ))
    interrupt = Message(role="user", content=(
        TextBlock(text="[Request interrupted by user]"),
    ))
    assert unresolved_wait_for_user(
        [first_ask, reply, second_ask, interrupt]) == (2, "wfu2")


def test_wait_for_user_resolved_with_real_reply_sends_to_llm():
    """A wait_for_user filled by the REAL reply (is_error=False) is resolved; the
    conversation continues normally."""
    reply = Message(role="user", content=(
        ToolResultBlock(tool_use_id="wfu1", content="merge it", is_error=False),))
    assert classify_resume([_assistant_waiting(), reply]) \
        is ResumeState.SEND_TO_LLM
