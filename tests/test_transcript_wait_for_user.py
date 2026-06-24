from imp.agents.runner import Message, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import (
    Transcript,
    balance_success_on_resume,
    repair_dangling_tool_use_on_resume,
    resolve_wait_for_user_on_resume,
)


class _Elog:
    def __init__(self):
        self.calls = []

    def append_user_message(self, *, agent_id, message):
        self.calls.append(message)


def _wfu_msg():
    return Message(role="assistant", content=(
        ToolUseBlock(id="wfu1", name="wait_for_user", input={"prompt": "merge?"}),
    ))


def test_repair_skips_wait_for_user(tmp_path):
    t = Transcript(tmp_path / "t.jsonl")
    msgs = [_wfu_msg()]
    assert repair_dangling_tool_use_on_resume(msgs, t) is None  # nothing synthesized
    assert len(msgs) == 1


def test_balance_skips_wait_for_user(tmp_path):
    t = Transcript(tmp_path / "t.jsonl")
    msgs = [_wfu_msg()]
    elog = _Elog()
    assert balance_success_on_resume(msgs, t, elog, "aid") is None
    assert len(msgs) == 1


def test_resolve_fills_real_reply(tmp_path):
    t = Transcript(tmp_path / "t.jsonl")
    msgs = [_wfu_msg()]
    elog = _Elog()
    injected = resolve_wait_for_user_on_resume(msgs, t, elog, "aid", "ship it")
    assert injected is not None and msgs[-1].role == "user"
    block = msgs[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "wfu1" and block.content == "ship it"
    assert block.is_error is False
    assert elog.calls == [injected]


def test_resolve_returns_none_without_wait_for_user(tmp_path):
    t = Transcript(tmp_path / "t.jsonl")
    msgs = [Message(role="assistant", content=(
        ToolUseBlock(id="b1", name="bash", input={"cmd": "ls"}),
    ))]
    elog = _Elog()
    assert resolve_wait_for_user_on_resume(msgs, t, elog, "aid", "x") is None
    assert len(msgs) == 1


def _interrupt_turn():
    from imp.agents.runner import TextBlock
    return Message(role="user", content=(
        TextBlock(text="[Request interrupted by user]"),))


def test_resolve_injects_reply_into_trailing_interrupt_turn(tmp_path):
    """When a [Request interrupted by user] turn trails the dangling ask, the real
    reply must be injected as a success-shaped tool_result at the HEAD of that
    user turn (Anthropic requires the result in the turn after the tool_use)."""
    from imp.agents.runner import TextBlock

    t = Transcript(tmp_path / "t.jsonl")
    msgs = [_wfu_msg(), _interrupt_turn()]
    elog = _Elog()

    injected = resolve_wait_for_user_on_resume(msgs, t, elog, "aid", "merge it")

    assert injected is not None
    assert len(msgs) == 2, "the trailing turn is repaired in place, not appended"
    head = msgs[-1].content[0]
    assert isinstance(head, ToolResultBlock)
    assert head.tool_use_id == "wfu1"
    assert head.content == "merge it"
    assert head.is_error is False
    # the original interrupt text is preserved after the injected result
    assert any(isinstance(b, TextBlock)
               and b.text == "[Request interrupted by user]"
               for b in msgs[-1].content)
    assert elog.calls == [injected]
    # persisted faithfully
    assert Transcript(tmp_path / "t.jsonl").load()[-1].content[0].content == "merge it"


def test_resolve_supersedes_placeholder_error_result(tmp_path):
    """A prior is_error 'awaiting user' placeholder for the ask is dropped and
    replaced by the real success-shaped reply — never two results for one id."""
    t = Transcript(tmp_path / "t.jsonl")
    placeholder = Message(role="user", content=(
        ToolResultBlock(tool_use_id="wfu1", content="awaiting user",
                        is_error=True),))
    msgs = [_wfu_msg(), placeholder]
    elog = _Elog()

    injected = resolve_wait_for_user_on_resume(msgs, t, elog, "aid", "go ahead")

    assert injected is not None
    results = [b for b in msgs[-1].content
               if isinstance(b, ToolResultBlock) and b.tool_use_id == "wfu1"]
    assert len(results) == 1, "exactly one tool_result for the ask id"
    assert results[0].content == "go ahead"
    assert results[0].is_error is False

    # persisted faithfully through the rewrite path
    reloaded = Transcript(tmp_path / "t.jsonl").load()
    persisted = [b for b in reloaded[-1].content
                 if isinstance(b, ToolResultBlock) and b.tool_use_id == "wfu1"]
    assert len(persisted) == 1
    assert persisted[0].content == "go ahead"
    assert persisted[0].is_error is False


def test_resolve_injects_into_first_trailing_turn_not_last(tmp_path):
    """When more than one user turn trails the ask, the reply must land in the
    FIRST following turn (Anthropic requires the result in the turn directly after
    the tool_use), never the last — proving resolve balances the exact ask
    classify_resume flagged, regardless of later turns."""
    from imp.agents.runner import TextBlock

    t = Transcript(tmp_path / "t.jsonl")
    msgs = [
        _wfu_msg(),
        _interrupt_turn(),
        Message(role="user", content=(TextBlock(text="please reconsider"),)),
    ]
    elog = _Elog()

    injected = resolve_wait_for_user_on_resume(msgs, t, elog, "aid", "merge it")

    assert injected is not None
    assert len(msgs) == 3, "no new turn is appended; the first trailing turn is repaired"
    head = msgs[1].content[0]
    assert isinstance(head, ToolResultBlock)
    assert head.tool_use_id == "wfu1"
    assert head.content == "merge it"
    assert head.is_error is False
    # the later wrapper turn is left untouched
    assert msgs[2].content[0].text == "please reconsider"
