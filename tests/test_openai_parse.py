from __future__ import annotations

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.providers.openai import (
    _extract_openai_diagnostics,
    _parse_openai_message,
    _serialize_openai_response,
)
from tests.openai_fakes import completion, message, tool_call


def test_serialize_then_parse_round_trips() -> None:
    comp = completion(message(content="hi",
                              tool_calls=[tool_call("t1", "read_file", {"p": "x"})]))
    raw = _serialize_openai_response(comp)
    assert "choices" in raw
    msg = _parse_openai_message(raw)
    assert msg.content[0] == TextBlock(text="hi")
    assert msg.content[1] == ToolUseBlock(id="t1", name="read_file", input={"p": "x"})


def test_refusal_becomes_warning() -> None:
    comp = completion(message(content=None, refusal="nope"))
    raw = _serialize_openai_response(comp)
    diag = _extract_openai_diagnostics(raw)
    assert any("refusal" in w for w in diag.block_warnings)
