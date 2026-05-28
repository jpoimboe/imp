from __future__ import annotations

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.providers.anthropic import (
    _extract_anthropic_diagnostics,
    _parse_anthropic_message,
    _serialize_anthropic_response,
)
from tests.anthropic_fakes import response, text, thinking, tool_use


def test_serialize_then_parse_round_trips_blocks() -> None:
    resp = response(text("hi"), tool_use("t1", "read_file", {"p": "x"}),
                    stop_reason="tool_use")
    raw = _serialize_anthropic_response(resp)
    assert isinstance(raw, dict)
    assert raw["stop_reason"] == "tool_use"
    msg = _parse_anthropic_message(raw)
    assert msg.role == "assistant"
    assert msg.content[0] == TextBlock(text="hi")
    assert msg.content[1] == ToolUseBlock(id="t1", name="read_file", input={"p": "x"})


def test_diagnostics_collects_thinking_and_skips_from_message() -> None:
    resp = response(thinking("reasoning"), text("answer"), stop_reason="end_turn")
    raw = _serialize_anthropic_response(resp)
    msg = _parse_anthropic_message(raw)
    assert all(getattr(b, "type", None) != "thinking" for b in msg.content)
    diag = _extract_anthropic_diagnostics(raw)
    assert diag.thinkings == ("reasoning",)


def test_unknown_block_becomes_warning() -> None:
    raw = {"content": [{"type": "mystery"}], "stop_reason": "end_turn",
           "usage": {}}
    diag = _extract_anthropic_diagnostics(raw)
    assert any("mystery" in w for w in diag.block_warnings)
