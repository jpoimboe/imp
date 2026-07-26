"""The error path classifies off the trailing message only.

`_last_text()` keeps its backward-scanning semantics for the success path's
`EndOfTurn(last_text=…)`; `_trailing_text()` is the error path's accessor.
"""

from __future__ import annotations

from types import SimpleNamespace

from imp.agents.runner import Message, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.providers.claude_code import ClaudeCodeSession
from imp.providers.codex_runner import CodexSession

_STALE_PROSE = "Failed to authenticate. API Error: 401 OAuth access token has been revoked"


def _stale_tail() -> list[Message]:
    """[user, assistant(auth prose), user reply] — the mada48 shape."""
    return [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text=_STALE_PROSE),)),
        Message(role="user", content=(TextBlock(text="looks good, proceed"),)),
    ]


def test_claude_trailing_text_is_empty_when_last_message_is_not_assistant() -> None:
    session = ClaudeCodeSession(None, None)
    session.messages = _stale_tail()
    assert session._trailing_text() == ""


def test_claude_trailing_text_is_empty_with_no_messages() -> None:
    assert ClaudeCodeSession(None, None)._trailing_text() == ""


def test_claude_trailing_text_returns_the_trailing_assistant_prose() -> None:
    session = ClaudeCodeSession(None, None)
    session.messages = [
        *_stale_tail(),
        Message(role="assistant", content=(TextBlock(text="API Error: 429"),)),
    ]
    assert session._trailing_text() == "API Error: 429"


def test_claude_last_text_keeps_backward_scanning_semantics() -> None:
    session = ClaudeCodeSession(None, None)
    session.messages = _stale_tail()
    assert session._last_text() == _STALE_PROSE


def test_codex_trailing_text_reads_the_per_turn_capture() -> None:
    session = CodexSession(None, None)
    session._proc_state = {"last_agent_text": "Invalid API key"}
    assert session._trailing_text() == "Invalid API key"


def test_codex_turn_boundary_clears_the_trailing_text() -> None:
    session = CodexSession(None, None)
    session._proc_state = {"last_agent_text": "Invalid API key"}
    session._state = SimpleNamespace(
        complete_step_called=True, complete_step_args={"status": "ok"})
    session.elog = InMemoryEventLog()
    session.aid = "step:p/s"

    session.send("continue", human=False)

    assert session._trailing_text() == ""
    assert session._terminal_error(session._trailing_text()) is None
