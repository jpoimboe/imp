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
