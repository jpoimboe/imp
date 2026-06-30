from __future__ import annotations

from imp.agents.runner import TextBlock, ThinkingBlock, ToolUseBlock
from imp.providers.anthropic import (
    _extract_anthropic_diagnostics,
    _parse_anthropic_message,
    _serialize_anthropic_response,
)
from tests.anthropic_fakes import (
    redacted_thinking, response, text, thinking, tool_use,
)


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


def test_parse_preserves_thinking_block_and_diagnostics_still_collects() -> None:
    resp = response(thinking("reasoning", signature="sig-1"), text("answer"),
                    stop_reason="end_turn")
    raw = _serialize_anthropic_response(resp)
    msg = _parse_anthropic_message(raw)
    # The thinking block is now PRESERVED in conversation history (it used to be
    # dropped), with its signature intact, ahead of the text block.
    assert msg.content[0] == ThinkingBlock(thinking="reasoning", signature="sig-1")
    assert isinstance(msg.content[1], TextBlock)
    # Diagnostics still independently collects the thinking text from raw.
    diag = _extract_anthropic_diagnostics(raw)
    assert diag.thinkings == ("reasoning",)


def test_unknown_block_becomes_warning() -> None:
    raw = {"content": [{"type": "mystery"}], "stop_reason": "end_turn",
           "usage": {}}
    diag = _extract_anthropic_diagnostics(raw)
    assert any("mystery" in w for w in diag.block_warnings)


def test_fallback_serialize_preserves_signature() -> None:
    # FakeResponse has no model_dump, so this exercises the introspection
    # fallback path — the one that used to drop the signature.
    resp = response(thinking("reasoning", signature="sig-xyz"),
                    stop_reason="end_turn")
    raw = _serialize_anthropic_response(resp)
    block = raw["content"][0]
    assert block["type"] == "thinking"
    assert block["signature"] == "sig-xyz"


def test_parse_redacted_thinking_block() -> None:
    resp = response(redacted_thinking("ENC=="), text("answer"),
                    stop_reason="end_turn")
    raw = _serialize_anthropic_response(resp)
    assert raw["content"][0] == {"type": "redacted_thinking", "data": "ENC=="}
    msg = _parse_anthropic_message(raw)
    assert msg.content[0] == ThinkingBlock(redacted_thinking="ENC==")


def test_redacted_thinking_is_not_a_diagnostic_warning() -> None:
    raw = {"content": [{"type": "redacted_thinking", "data": "ENC=="}],
           "stop_reason": "end_turn", "usage": {}}
    diag = _extract_anthropic_diagnostics(raw)
    assert diag.block_warnings == ()
