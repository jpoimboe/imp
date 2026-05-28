from __future__ import annotations

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.providers.claude_code import (
    _extract_claude_code_diagnostics,
    _parse_claude_code_message,
    _serialize_claude_code_message,
)
from tests.cc_fakes import FakeAssistantMessage, FakeTextBlock, FakeToolUseBlock


def test_serialize_then_parse_assistant() -> None:
    sdk = FakeAssistantMessage(content=[
        FakeTextBlock(text="hi"),
        FakeToolUseBlock(id="t1", name="mcp__imp__read_file", input={"p": "x"}),
    ])
    raw = _serialize_claude_code_message(sdk)
    msg = _parse_claude_code_message(raw)
    assert msg.content[0] == TextBlock(text="hi")
    # tool name canonicalized (mcp__imp__ prefix stripped)
    assert msg.content[1] == ToolUseBlock(id="t1", name="read_file", input={"p": "x"})


def test_diagnostics_collects_thinking() -> None:
    from tests.cc_fakes import FakeThinkingBlock
    sdk = FakeAssistantMessage(content=[FakeThinkingBlock(thinking="reasoning"),
                                        FakeTextBlock(text="answer")])
    raw = _serialize_claude_code_message(sdk)
    diag = _extract_claude_code_diagnostics(raw)
    assert diag.thinkings == ("reasoning",)
