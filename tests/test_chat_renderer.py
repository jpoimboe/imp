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


def test_script_gate_result_summarized() -> None:
    text = _render([
        {"type": "script_gate_result", "payload": {
            "exit": 0, "stdout": "ok\n" * 100, "stderr": "",
        }},
    ])
    assert "exit=0" in text
    # Don't dump the stdout
    assert text.count("ok") < 10


# --- Bugs C/G: token-buffer structured-response suppression ---


def test_structured_response_token_stream_is_discarded() -> None:
    """If the agent's text turn streams the response_format JSON envelope
    character-by-character, the renderer must NOT show it as [agent] ..."""
    text = _render([
        {"type": "agent_token", "payload": {"text": '{"sta'}},
        {"type": "agent_token", "payload": {"text": 'tus":"Su'}},
        {"type": "agent_token", "payload": {"text": 'ccess"}'}},
        {"type": "tool_call", "payload": {"name": "read_file", "args": {}}},
    ])
    assert "[agent]" not in text
    assert "status" not in text
    assert "read_file" in text  # other events still render


def test_real_text_token_stream_renders_and_suppresses_message() -> None:
    """Real text tokens flush to [agent] ..., and the matching agent_message
    event (which arrives in updates-mode for the same content) is suppressed."""
    text = _render([
        {"type": "agent_token", "payload": {"text": "What would "}},
        {"type": "agent_token", "payload": {"text": "you like to build?"}},
        {"type": "agent_message", "payload": {"text": "What would you like to build?"}},
    ])
    # Exactly one rendering, no duplicate
    assert text.count("What would you like to build?") == 1
    assert "[agent]" in text
