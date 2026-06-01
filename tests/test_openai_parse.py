from __future__ import annotations

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.providers.openai import (
    _extract_responses_diagnostics,
    _parse_responses_message,
    _serialize_responses_response,
)
from tests.openai_fakes import resp_function_call, resp_message, resp_response


def test_serialize_then_parse_round_trips() -> None:
    resp = resp_response(
        resp_message("hi"),
        resp_function_call("t1", "read_file", '{"p": "x"}'),
    )
    raw = _serialize_responses_response(resp)
    assert "output" in raw
    msg = _parse_responses_message(raw)
    assert msg.content[0] == TextBlock(text="hi")
    assert msg.content[1] == ToolUseBlock(id="t1", name="read_file", input={"p": "x"})


def test_bad_function_args_become_warning() -> None:
    resp = resp_response(resp_function_call("t1", "read_file", "{not json}"))
    raw = _serialize_responses_response(resp)
    diag = _extract_responses_diagnostics(raw)
    assert any("not JSON" in w for w in diag.block_warnings)
