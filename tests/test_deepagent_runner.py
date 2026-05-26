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
    from collections.abc import Iterator
    from typing import Any

    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatResult

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
