from __future__ import annotations

from imp.cli.render_helpers import (
    conversation_prose,
    render_llm_response,
    user_message_text,
)


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


def test_prompt_message_caps_hr_at_display_width() -> None:
    from imp.cli import palette as pal
    from imp.cli.prompt import _prompt_message_text

    # Wrap width huge, display width small: the rendered question's HR caps at 18.
    msg = _prompt_message_text("Pick one\n\n---", color=True, width=10000,
                               display_width=18)
    assert pal.rule(18, enabled=True) in msg


def _llm_event(text: str) -> dict:
    return {
        "type": "llm_response",
        "payload": {
            "provider": "anthropic",
            "raw": {
                "content": [
                    {"type": "text", "text": text},
                    {"type": "tool_use", "id": "t1", "name": "bash",
                     "input": {"cmd": "grep TOOLNOISE"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    }


def test_conversation_prose_concatenates_prose_in_order() -> None:
    events = [
        {"type": "opening_intent", "payload": {"text": "make search better"}},
        {"type": "user_message",
         "payload": {"content": [{"type": "text", "text": "add a 300s timeout"}],
                     "human": True}},
        _llm_event("sure, the 300s deadline is wired"),
    ]
    prose = conversation_prose(events)
    assert prose == (
        "make search better\n"
        "add a 300s timeout\n"
        "sure, the 300s deadline is wired")


def test_conversation_prose_excludes_tool_and_nonprose_events() -> None:
    events = [
        {"type": "step_context", "payload": {"step": "spec"}},
        {"type": "tool_call", "payload": {"name": "bash", "input": {"cmd": "SECRET"}}},
        {"type": "tool_result", "payload": {"content": "SECRET output"}},
        _llm_event("visible assistant prose"),
    ]
    prose = conversation_prose(events)
    assert prose == "visible assistant prose"   # only the assistant TextBlock text
    assert "SECRET" not in prose                 # tool call/result payloads excluded
    assert "TOOLNOISE" not in prose              # tool_use block text excluded


def test_conversation_prose_skips_malformed_without_raising() -> None:
    events = [
        {"type": "opening_intent", "payload": {"text": "first"}},
        {"_malformed": True, "raw": "NOT_JSON"},              # sentinel, no type/payload
        "NOT_AN_EVENT_DICT",                                  # non-dict event
        {"type": "llm_response", "payload": "not-a-dict-payload"},   # non-dict payload value
        {"type": "llm_response", "payload": {"raw": "not-a-dict"}},  # would raise if parsed
        {"type": "user_message",
         "payload": {"content": [{"type": "text", "text": "second"}],
                     "human": True}},
    ]
    assert conversation_prose(events) == "first\nsecond"


def test_conversation_prose_includes_human_user_message() -> None:
    events = [
        {"type": "user_message",
         "payload": {"content": [{"type": "text", "text": "typed human reply"}],
                     "human": True}},
    ]
    assert conversation_prose(events) == "typed human reply"


def test_conversation_prose_excludes_nonhuman_user_message() -> None:
    events = [
        {"type": "user_message",
         "payload": {"content": [{"type": "text", "text": "INJECTED cover-check prompt"}],
                     "human": False}},
        {"type": "user_message",
         "payload": {"content": [{"type": "text", "text": "genuine human reply"}],
                     "human": True}},
    ]
    prose = conversation_prose(events)
    assert prose == "genuine human reply"    # only the human turn indexed
    assert "INJECTED" not in prose           # injected prompt excluded


def test_conversation_prose_treats_missing_human_field_as_nonhuman() -> None:
    events = [
        {"type": "user_message",
         "payload": {"content": [{"type": "text", "text": "no human field here"}]}},
    ]
    assert conversation_prose(events) == ""  # absent human field -> excluded
