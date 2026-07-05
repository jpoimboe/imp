from __future__ import annotations

from imp.agents.runner import (
    ImagePart,
    Message,
    TextBlock,
    TextPart,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.providers.anthropic import (
    _extract_anthropic_diagnostics,
    _message_to_anthropic,
    _parse_anthropic_message,
    _serialize_anthropic_response,
)
from tests.anthropic_fakes import (
    redacted_thinking,
    response,
    text,
    thinking,
    tool_use,
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


def test_message_to_anthropic_emits_thinking_first_with_signature() -> None:
    msg = Message(role="assistant", content=(
        ThinkingBlock(thinking="reasoning", signature="sig-1"),
        ToolUseBlock(id="t1", name="read_file", input={"p": "x"}),
    ))
    wire = _message_to_anthropic(msg)["content"]
    assert wire[0] == {"type": "thinking", "thinking": "reasoning",
                       "signature": "sig-1"}
    assert wire[1]["type"] == "tool_use"


def test_message_to_anthropic_emits_redacted_thinking() -> None:
    msg = Message(role="assistant", content=(
        ThinkingBlock(redacted_thinking="ENC=="),
        TextBlock(text="answer"),
    ))
    wire = _message_to_anthropic(msg)["content"]
    assert wire[0] == {"type": "redacted_thinking", "data": "ENC=="}
    assert wire[1]["type"] == "text"


def test_tool_use_turn_with_thinking_round_trips_signed() -> None:
    # Full raw -> parse -> request round-trip: a tool-use turn whose signed
    # thinking block must survive verbatim and stay ahead of the tool_use.
    raw = {"content": [
        {"type": "thinking", "thinking": "deciding", "signature": "sig-rt"},
        {"type": "tool_use", "id": "c1", "name": "complete_step",
         "input": {"status": "Success"}},
    ], "stop_reason": "tool_use", "usage": {}}
    msg = _parse_anthropic_message(raw)
    wire = _message_to_anthropic(msg)["content"]
    assert wire[0] == {"type": "thinking", "thinking": "deciding",
                       "signature": "sig-rt"}
    assert wire[1] == {"type": "tool_use", "id": "c1", "name": "complete_step",
                       "input": {"status": "Success"}}


def test_message_to_anthropic_tolerates_no_thinking_block() -> None:
    # Adaptive-skip turn: no thinking block at all still serializes cleanly.
    msg = Message(role="assistant", content=(
        TextBlock(text="answer"),
        ToolUseBlock(id="t1", name="read_file", input={"p": "x"}),
    ))
    wire = _message_to_anthropic(msg)["content"]
    assert [b["type"] for b in wire] == ["text", "tool_use"]


def test_message_to_anthropic_list_content_emits_image_block() -> None:
    parts = [
        TextPart(text="shot.png — 300×200 PNG"),
        ImagePart(media_type="image/png", data="aGVsbG8=", width=300,
                  height=200, blob="ab12.png", nbytes=5),
    ]
    msg = Message(role="user",
                  content=(ToolResultBlock(tool_use_id="t1", content=parts),))
    out = _message_to_anthropic(msg)
    tr = out["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "t1"
    assert isinstance(tr["content"], list)
    assert tr["content"][0] == {"type": "text", "text": "shot.png — 300×200 PNG"}
    assert tr["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
    }


def test_message_to_anthropic_str_content_unchanged() -> None:
    msg = Message(role="user",
                  content=(ToolResultBlock(tool_use_id="t1", content="plain text",
                                           is_error=True),))
    out = _message_to_anthropic(msg)
    tr = out["content"][0]
    assert tr["content"] == "plain text"
    assert tr["is_error"] is True
