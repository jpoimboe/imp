from __future__ import annotations

import pytest

from imp.agents.runner import Message, TextBlock, ToolUseBlock
from imp.cli.inert_resume import render_pending_question


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_render_pending_question_returns_last_assistant_text() -> None:
    msgs = [
        Message(role="user", content=(TextBlock(text="start"),)),
        Message(role="assistant", content=(TextBlock(text="Which DB should I use?"),)),
    ]
    assert render_pending_question(msgs) == "Which DB should I use?"


def test_render_pending_question_falls_back_to_preceding_prose() -> None:
    # The final assistant turn carries no text -> walk back to the prior prose.
    msgs = [
        Message(role="assistant", content=(TextBlock(text="earlier question?"),)),
        Message(role="user", content=(TextBlock(text="ok"),)),
        Message(role="assistant", content=()),
    ]
    assert render_pending_question(msgs) == "earlier question?"


def test_render_pending_question_falls_back_past_tool_use_only_turn() -> None:
    msgs = [
        Message(role="assistant", content=(TextBlock(text="earlier question?"),)),
        Message(role="user", content=(TextBlock(text="ok"),)),
        Message(role="assistant", content=(ToolUseBlock(id="t1", name="bash", input={}),)),
    ]
    assert render_pending_question(msgs) == "earlier question?"


def test_render_pending_question_none_when_no_assistant_text() -> None:
    msgs = [Message(role="user", content=(TextBlock(text="hi"),))]
    assert render_pending_question(msgs) is None
