from __future__ import annotations

from imp.cli.render_helpers import (
    conversation_prose,
    event_text,
    events_content,
    render_agent_message,
    user_message_text,
)


def test_render_agent_message_extracts_text_and_tool_uses() -> None:
    payload = {
        "provider": "anthropic", "model": "m",
        "raw": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"p": 1}},
        ], "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 3}},
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "stop_reason": "tool_use",
    }
    r = render_agent_message(payload)
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


def test_question_block_caret_escapes_agent_authored_control_bytes() -> None:
    from imp.cli.render_text import render_question_block

    # An OSC window-title sequence in agent text must never reach the terminal.
    text = "\n".join(render_question_block(
        "Ready? \x1b]0;pwned\x07 now", 60, 0, color=True))
    assert "\x1b]" not in text
    assert "^[]0;pwned^G" in text


def test_question_block_keeps_a_colored_inline_sample() -> None:
    from imp.cli.render_text import _strip_sgr, render_question_block

    # The escape pass must leave a bare code armable, so the prompt shows the same
    # colored sample the scrollback does — arming it first would print `^[[1;33m`.
    text = "\n".join(render_question_block(
        "The gauge reads `[1;33m78%[0m` — ok?", 60, 0, color=True))
    assert "\x1b[1;33m78%" in text
    assert "[1;33m" not in _strip_sgr(text)


def test_question_block_keeps_fence_color_like_the_scrollback() -> None:
    from imp.cli.render_text import _strip_sgr, render_question_block

    # A model cannot type ESC, so a mockup's codes arrive bare and only the PROSE
    # escape restores them; the absolute one leaves the fence body literal.
    text = "\n".join(render_question_block(
        "Here it is:\n\n```\n[1;33m▌ imp[0m [36mplanning[0m\n```\n",
        70, 0, color=True))
    assert "\x1b[1;33m▌ imp\x1b[0m" in text
    assert "[1;33m" not in _strip_sgr(text)
    assert "^[[" not in text


def test_prompt_message_caps_hr_at_display_width() -> None:
    from imp.cli import palette as pal
    from imp.cli.prompt import _prompt_message_text

    # Wrap width huge, display width small: the rendered question's HR caps at 18.
    msg = _prompt_message_text("Pick one\n\n---", color=True, width=10000,
                               display_width=18)
    assert pal.rule(18, enabled=True) in msg


def _llm_event(text: str) -> dict:
    return {
        "type": "agent_message",
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
        {"type": "user_message", "agent_id": None,
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "make search better"}]}},
        {"type": "user_message",
         "payload": {"subtype": "reply", "role": "user",
                     "content": [{"type": "text", "text": "add a 300s timeout"}]}},
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
        {"type": "user_message",
         "payload": {"subtype": "intent",
                     "content": [{"type": "text", "text": "first"}]}},
        {"_malformed": True, "raw": "NOT_JSON"},              # sentinel, no type/payload
        "NOT_AN_EVENT_DICT",                                  # non-dict event
        {"type": "agent_message", "payload": "not-a-dict-payload"},   # non-dict payload value
        {"type": "agent_message", "payload": {"raw": "not-a-dict"}},  # would raise if parsed
        {"type": "user_message",
         "payload": {"subtype": "reply",
                     "content": [{"type": "text", "text": "second"}]}},
    ]
    assert conversation_prose(events) == "first\nsecond"


def test_conversation_prose_includes_human_user_message() -> None:
    events = [
        {"type": "user_message",
         "payload": {"subtype": "reply",
                     "content": [{"type": "text", "text": "typed human reply"}]}},
    ]
    assert conversation_prose(events) == "typed human reply"


def test_conversation_prose_excludes_nonhuman_user_message() -> None:
    events = [
        {"type": "engine_message",
         "payload": {"subtype": "agent_prompt", "role": "user",
                     "content": [{"type": "text", "text": "INJECTED cover-check prompt"}]}},
        {"type": "user_message",
         "payload": {"subtype": "reply", "role": "user",
                     "content": [{"type": "text", "text": "genuine human reply"}]}},
    ]
    prose = conversation_prose(events)
    assert prose == "genuine human reply"    # only the human turn indexed
    assert "INJECTED" not in prose           # injected prompt excluded


def test_conversation_prose_treats_missing_human_field_as_nonhuman() -> None:
    events = [
        {"type": "engine_message",
         "payload": {"subtype": "agent_prompt", "role": "user",
                     "content": [{"type": "text", "text": "no human field here"}]}},
    ]
    assert conversation_prose(events) == ""  # engine turns are not conversation


def test_events_content_collects_structured_payload_strings() -> None:
    events = [
        {"type": "tool_call",
         "payload": {"name": "load_skill", "input": {"name": "imp-tool-reference"}}},
        {"type": "tool_result",
         "payload": {"id": "t1", "content": "Traceback: BOOMERROR at line 5"}},
        {"type": "skill_load", "payload": {"name": "imp-knowledge", "source": "internal"}},
    ]
    text = events_content(events)
    assert "load_skill" in text
    assert "imp-tool-reference" in text
    assert "BOOMERROR" in text
    assert "imp-knowledge" in text


def test_events_content_excludes_step_context_boilerplate() -> None:
    events = [
        {"type": "step_context",
         "payload": {"provider": "anthropic", "model": "claude-x",
                     "system_prompt": "BOILERPLATE_PROMPT",
                     "tools_schema": [{"name": "SCHEMANOISE"}]}},
    ]
    text = events_content(events)
    assert "anthropic" in text
    assert "claude-x" in text
    assert "BOILERPLATE_PROMPT" not in text
    assert "SCHEMANOISE" not in text


def test_events_content_collects_nested_list_content_blocks() -> None:
    events = [
        {"type": "tool_result",
         "payload": {"id": "t1", "content": [{"type": "text", "text": "NESTEDHIT"}]}},
    ]
    assert "NESTEDHIT" in events_content(events)


def test_events_content_includes_agent_message_raw_text() -> None:
    events = [
        {"type": "agent_message",
         "payload": {"provider": "anthropic",
                     "raw": {"content": [{"type": "text", "text": "REASONING_TEXT"}]}}},
    ]
    assert "REASONING_TEXT" in events_content(events)


def test_events_content_skips_non_dict_and_missing_payload() -> None:
    events = [
        "NOT_A_DICT",
        {"type": "x"},
        {"type": "y", "payload": "notadict"},
        {"type": "z", "payload": {"k": "KEPT"}},
    ]
    text = events_content(events)
    assert "KEPT" in text
    assert "NOT_A_DICT" not in text


def test_event_text_collects_payload_strings() -> None:
    ev = {"type": "tool_call",
          "payload": {"name": "load_skill", "input": {"name": "imp-tool-reference"}}}
    text = event_text(ev)
    assert "load_skill" in text
    assert "imp-tool-reference" in text


def test_event_text_excludes_step_context_boilerplate() -> None:
    ev = {"type": "step_context",
          "payload": {"provider": "anthropic", "model": "claude-x",
                      "system_prompt": "BOILERPLATE_PROMPT",
                      "tools_schema": [{"name": "SCHEMANOISE"}]}}
    text = event_text(ev)
    assert "anthropic" in text
    assert "claude-x" in text
    assert "BOILERPLATE_PROMPT" not in text
    assert "SCHEMANOISE" not in text


def test_event_text_empty_for_non_dict_or_missing_payload() -> None:
    assert event_text("NOT_A_DICT") == ""
    assert event_text({"type": "x"}) == ""
    assert event_text({"type": "y", "payload": "notadict"}) == ""


def test_events_content_still_matches_after_refactor() -> None:
    events = [
        {"type": "tool_result", "payload": {"id": "t1", "content": "STACKTRACE_XYZ"}},
        {"type": "step_context",
         "payload": {"system_prompt": "PROMPTNOISE", "model": "m"}},
    ]
    text = events_content(events)
    assert "STACKTRACE_XYZ" in text
    assert "m" in text
    assert "PROMPTNOISE" not in text


def test_conversation_prose_triad_vocabulary() -> None:
    events = [
        {"type": "user_message",
         "payload": {"subtype": "intent",
                     "content": [{"type": "text", "text": "make search better"}]}},
        {"type": "user_message",
         "payload": {"subtype": "reply",
                     "content": [{"type": "text", "text": "add a 300s timeout"}]}},
        {"type": "user_message",
         "payload": {"subtype": "interject",
                     "content": [{"type": "text", "text": "wait, use sqlite"}]}},
        {"type": "engine_message",
         "payload": {"subtype": "agent_prompt",
                     "content": [{"type": "text", "text": "FRAMEWORK BODY"}]}},
        {"type": "agent_message",
         "payload": {"provider": "anthropic",
                     "raw": {"content": [{"type": "text",
                                          "text": "the deadline is wired"}],
                             "stop_reason": "end_turn", "usage": {}}}},
    ]
    prose = conversation_prose(events)
    assert prose == (
        "make search better\n"
        "add a 300s timeout\n"
        "wait, use sqlite\n"
        "the deadline is wired")
    assert "FRAMEWORK BODY" not in prose      # engine turns are not conversation
