"""Tests for _had_exchange with migrated wait_for_user shape.

Old logs used tool_call/tool_result for wfu; migrated shape uses
awaiting_user_input + user_message.  Verify the current arms handle both
correctly without any wfu special-casing.
"""
from imp.cli.leave import _had_exchange


class _Log:
    def __init__(self, events): self._events = events
    def read(self): return list(self._events)


def test_had_exchange_true_on_migrated_awaiting_then_user_message():
    log = _Log([
        {"type": "awaiting_user_input", "payload": {"prompt": ""}},
        {"type": "user_message", "payload": {"role": "user",
                                             "content": [{"type": "text", "text": "go"}]}},
    ])
    assert _had_exchange(log) is True


def test_had_exchange_false_on_awaiting_with_no_reply():
    log = _Log([{"type": "awaiting_user_input", "payload": {"prompt": ""}}])
    assert _had_exchange(log) is False
