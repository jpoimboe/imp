from __future__ import annotations

from imp.cli.render_helpers import render_llm_response, user_message_text


def test_render_llm_response_extracts_text_and_tool_uses() -> None:
    payload = {
        "provider": "anthropic", "model": "m",
        "raw": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"p": 1}},
        ], "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 3}},
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "stop_reason": "tool_use",
    }
    r = render_llm_response(payload)
    assert "hello" in r.texts
    assert r.tool_uses[0].name == "read_file"
    assert r.stop_reason == "tool_use"
    assert r.usage["input_tokens"] == 10


def test_user_message_text_mixed_blocks() -> None:
    payload = {
        "role": "user",
        "content": [
            {"type": "text", "text": "hello "},
            {"type": "image", "source": {"type": "base64", "data": "..."}},
            {"type": "text", "text": "world"},
        ],
    }
    assert user_message_text(payload) == "hello world"


def test_user_message_text_empty_content() -> None:
    # explicit content=[] and content=None should both return empty string
    assert user_message_text({"role": "user", "content": []}) == ""
    assert user_message_text({"role": "user", "content": None}) == ""
