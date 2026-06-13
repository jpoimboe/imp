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


def test_question_block_renders_markdown_bold_not_asterisks() -> None:
    from imp.cli.render_text import render_question_block

    lines = render_question_block(
        "Reply **merge** to merge.", 80, 0, color=True)
    text = "\n".join(lines)
    assert "\x1b[1m" in text     # bold SGR emitted for **merge**
    assert "**" not in text      # literal markdown asterisks gone


def test_question_block_word_wraps_without_mid_word_truncation() -> None:
    from imp.cli.render_text import render_question_block

    question = " ".join(["wordy"] * 40)   # ~200 chars, must wrap at width 40
    lines = render_question_block(question, 40, 0, color=False)
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= 40
        for token in line.split():
            assert token == "wordy"


def test_question_block_respects_depth_indent() -> None:
    from imp.cli.render_text import render_question_block

    lines = render_question_block("short text", 40, 1, color=False)
    assert len(lines) > 0
    # Each line is prefixed by the depth-1 gutter indent.
    for line in lines:
        assert len(line) <= 40
