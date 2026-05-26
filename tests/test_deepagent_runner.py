"""DeepAgentRunner — placeholder; full coverage lands in Task 23 (mocked e2e)
after tests/fakes.py exists (Task 22)."""

from __future__ import annotations


def test_module_imports_cleanly() -> None:
    """Basic smoke: the module can be imported without crashing."""
    from imp.agents.deepagent_runner import DeepAgentRunner

    assert DeepAgentRunner is not None


def test_runner_emits_agent_token_events_in_order(monkeypatch) -> None:
    """A fake stream that yields message-mode token chunks should produce
    agent_token events in arrival order, with a final agent_message
    aggregating them."""

    from langchain_core.messages import AIMessage, AIMessageChunk

    from imp.agents.deepagent_runner import _process_stream_chunk

    # Simulate two updates-mode chunks (message accumulation) + final structured
    tokens: list[str] = []
    messages: list[str] = []

    chunks = [
        ("messages", (AIMessageChunk(content="hello "), {})),
        ("messages", (AIMessageChunk(content="world"), {})),
        ("updates", {"agent": {"messages": [AIMessage(content="hello world")]}}),
    ]
    for mode, payload in chunks:
        events = list(_process_stream_chunk(mode, payload, suppress=[]))
        for e in events:
            if e["type"] == "agent_token":
                tokens.append(e["payload"]["text"])
            elif e["type"] == "agent_message":
                messages.append(e["payload"]["text"])
    assert tokens == ["hello ", "world"]
    assert messages == ["hello world"]


def test_process_stream_chunk_filters_empty_token() -> None:
    """Empty-content message chunks produce no agent_token event."""
    from langchain_core.messages import AIMessageChunk

    from imp.agents.deepagent_runner import _process_stream_chunk

    events = list(_process_stream_chunk("messages", (AIMessageChunk(content=""), {}), []))
    assert events == []


# --- Bug A: _extract_text multimodal content shapes ---


def test_extract_text_handles_plain_str() -> None:
    from imp.agents.deepagent_runner import _extract_text
    assert _extract_text("hello") == "hello"


def test_extract_text_handles_anthropic_blocks() -> None:
    from imp.agents.deepagent_runner import _extract_text
    content = [
        {"type": "text", "text": "Looking at the code."},
        {"type": "tool_use", "id": "1", "name": "read_file", "input": {}},
        {"type": "text", "text": " Let me check more."},
    ]
    assert _extract_text(content) == "Looking at the code. Let me check more."


def test_extract_text_drops_thinking_blocks() -> None:
    from imp.agents.deepagent_runner import _extract_text
    content = [{"type": "thinking", "thinking": "internal reasoning..."}]
    assert _extract_text(content) == ""


def test_extract_text_handles_empty_inputs() -> None:
    from imp.agents.deepagent_runner import _extract_text
    assert _extract_text("") == ""
    assert _extract_text([]) == ""
    assert _extract_text(None) == ""


# --- Bug D: _looks_like_structured_response + _translate_message filter ---


def test_looks_like_structured_response_recognizes_status_envelopes() -> None:
    from imp.agents.deepagent_runner import _looks_like_structured_response
    assert _looks_like_structured_response('{"status": "Success"}')
    assert _looks_like_structured_response('{"status": "Error", "intent": "x"}')
    # Whitespace + nesting tolerated
    assert _looks_like_structured_response('  {"status": "Success", "data": {"a": 1}}  ')


def test_looks_like_structured_response_rejects_non_envelopes() -> None:
    from imp.agents.deepagent_runner import _looks_like_structured_response
    assert not _looks_like_structured_response("Just plain text")
    assert not _looks_like_structured_response('{"foo": "bar"}')  # no status
    assert not _looks_like_structured_response("{not json}")
    assert not _looks_like_structured_response("")


def test_translate_message_drops_structured_response_text() -> None:
    """AIMessage whose entire content is the structured-response JSON does
    NOT emit an agent_message event (the engine emits agent_output instead)."""
    from langchain_core.messages import AIMessage

    from imp.agents.deepagent_runner import _translate_message
    msg = AIMessage(content='{"status": "Success", "summary": "ok"}')
    events = _translate_message(msg, [])
    assert events == []
