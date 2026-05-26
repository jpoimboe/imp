"""ChatRenderer: stream tokens, render labeled events, surface errors."""

from __future__ import annotations

from io import StringIO

from imp.cli.chat import ChatRenderer


def _render(events: list[dict]) -> str:
    out = StringIO()
    r = ChatRenderer(out=out, spinner=lambda _msg: lambda: None)
    for e in events:
        r.handle(e)
    r.close()
    return out.getvalue()


def test_tokens_accumulate_then_newline() -> None:
    text = _render([
        {"type": "agent_token", "payload": {"text": "hello "}},
        {"type": "agent_token", "payload": {"text": "world"}},
        {"type": "agent_message", "payload": {"text": "hello world"}},
    ])
    assert "hello world" in text
    # exactly one occurrence — token line should not duplicate with message
    assert text.count("hello world") == 1


def test_tool_call_labeled() -> None:
    text = _render([
        {"type": "tool_call", "payload": {
            "id": "1", "name": "read_file", "args": {"path": "/a"},
        }},
        {"type": "tool_result", "payload": {
            "tool_call_id": "1", "result": "ok",
        }},
    ])
    assert "read_file" in text
    assert "/a" in text


def test_step_started_shows_step_name() -> None:
    text = _render([
        {"type": "step_started", "payload": {
            "step": "spec-writer", "phase": "spec", "model_role": "spec",
            "interactive": False,
        }},
    ])
    assert "spec-writer" in text
    assert "spec" in text


def test_error_event_is_prominent() -> None:
    text = _render([
        {"type": "error", "payload": {"message": "boom"}},
    ])
    assert "ERROR" in text or "error" in text
    assert "boom" in text


def test_agent_output_keys_summarized() -> None:
    text = _render([
        {"type": "agent_output", "payload": {
            "status": "Success", "verdict": "approve",
        }},
    ])
    assert "Success" in text
    assert "approve" in text


def test_user_input_echoed() -> None:
    text = _render([
        {"type": "user_input", "payload": {"text": "I want feature X"}},
    ])
    assert "feature X" in text
