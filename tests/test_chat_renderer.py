from __future__ import annotations

from io import StringIO

from imp.cli.chat import ChatRenderer


def test_renderer_agent_message_block() -> None:
    out = StringIO()
    r = ChatRenderer(out=out)
    r.handle({"type": "agent_message", "payload": {
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
    }})
    assert "hello" in out.getvalue()


def test_renderer_tool_call_visible() -> None:
    out = StringIO()
    r = ChatRenderer(out=out)
    r.handle({"type": "tool_call", "payload": {
        "id": "u1", "name": "read_file", "input": {"path": "/project/x"},
    }})
    assert "read_file" in out.getvalue()
    assert "/project/x" in out.getvalue()


def test_renderer_agent_output_marks_completion() -> None:
    out = StringIO()
    r = ChatRenderer(out=out)
    r.handle({"type": "agent_output", "payload": {"status": "Success", "note": "ok"}})
    assert "Success" in out.getvalue()
