from __future__ import annotations

import inspect

import imp.engine.executor as ex


def test_executor_emits_user_message_intent_not_opening_intent() -> None:
    src = inspect.getsource(ex)
    assert '"opening_intent"' not in src           # old run-level type gone
    assert 'subtype="intent"' in src               # emitted as user_message/intent
    assert "append_user_message(" in src
