"""Contract tests: feed REAL `openai_codex_sdk` item models through
`process_event` (not hand-rolled fakes).

Why this file exists: the duck-typed fakes in `codex_fakes.py` previously
modeled `McpToolCallItem.name` and `FileChangeItem.path`, but the real SDK uses
`tool`/`server` and `changes:[{path,kind}]`. The runner read the fake field
names, so unit tests and runner agreed on a fiction and every live codex
`tool_call` was recorded with an empty name (and native-edit details showed "?").
These tests construct the actual SDK pydantic models so any future field drift
fails here instead of surfacing in a live dogfood.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("openai_codex_sdk") is None,
    reason="openai_codex_sdk not importable",
)

from openai_codex_sdk.types import (  # noqa: E402
    AgentMessageItem,
    ErrorItem,
    FileChangeItem,
    FileUpdateChange,
    ItemCompletedEvent,
    McpToolCallItem,
    McpToolCallResult,
)


def _feed(item: Any, state: dict[str, Any] | None = None) -> tuple[InMemoryEventLog, dict]:
    elog = InMemoryEventLog()
    st = state if state is not None else {}
    process_event(
        ItemCompletedEvent(type="item.completed", item=item),
        elog=elog, agent_id="step:t", state=st, provider="codex", model="gpt-5.5",
    )
    return elog, st


def test_real_mcp_tool_call_records_tool_name_and_result() -> None:
    item = McpToolCallItem(
        id="c1", type="mcp_tool_call", server="imp", tool="edit_file",
        arguments={"path": "a.txt", "old": "x", "new": "y"},
        result=McpToolCallResult(
            content=[{"type": "text", "text": "replaced 1 occurrence in a.txt"}],
            structured_content=None,
        ),
        status="completed",
    )
    elog, _ = _feed(item)
    events = list(elog.read())
    calls = [e for e in events if e["type"] == "tool_call"]
    assert len(calls) == 1, events
    # The whole point: the real field is `tool`, and the name must be captured.
    assert calls[0]["payload"]["name"] == "edit_file", calls[0]
    assert calls[0]["payload"]["input"]["old"] == "x"
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    # And the result content is the tool's text, not a pydantic repr.
    assert results[0]["payload"]["content"] == "replaced 1 occurrence in a.txt"
    assert results[0]["payload"]["error"] is False


def test_real_mcp_tool_call_complete_step_capture_fires() -> None:
    # Previously dead: the `if name == "complete_step"` branch never matched
    # because name was "". With `tool` read correctly it must capture.
    item = McpToolCallItem(
        id="c2", type="mcp_tool_call", server="imp", tool="complete_step",
        arguments={"word": "banana"}, result=None, status="completed",
    )
    _, st = _feed(item)
    assert st.get("complete_step_called") is True
    assert st.get("complete_step_args") == {"word": "banana"}


def test_real_mcp_tool_call_error_marks_is_error() -> None:
    from openai_codex_sdk.types import McpToolCallError
    item = McpToolCallItem(
        id="c3", type="mcp_tool_call", server="imp", tool="edit_file",
        arguments={}, error=McpToolCallError(message="boom"), status="failed",
    )
    elog, _ = _feed(item)
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["error"] is True
    assert "boom" in results[0]["payload"]["content"]


def test_real_file_change_native_violation_detail_has_path() -> None:
    item = FileChangeItem(
        id="f1", type="file_change",
        changes=[FileUpdateChange(path="/proj/native.txt", kind="update")],
        status="completed",
    )
    elog, st = _feed(item)
    assert st["native_violation"] is not None
    # Detail must carry the real path (read from `changes`, not a missing `.path`).
    assert "/proj/native.txt" in st["native_violation"]["detail"]
    assert "/proj/native.txt" in str(
        [e for e in elog.read() if e["type"] == "runner_warning"]
    )


def test_real_error_item_is_diagnostic_not_unhandled() -> None:
    item = ErrorItem(id="e1", type="error",
                     message="`[features].web_search_request` is deprecated")
    elog, _ = _feed(item)
    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert len(warns) == 1
    # Must surface the real message, flagged — NOT "unhandled codex item type".
    assert warns[0]["payload"].get("codex_error_item") is True
    assert "unhandled" not in str(warns[0]["payload"]).lower()
    assert "web_search_request" in warns[0]["payload"]["message"]


def test_real_agent_message_records_llm_response() -> None:
    item = AgentMessageItem(id="a1", type="agent_message", text="the answer is 42")
    elog, st = _feed(item)
    resp = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(resp) == 1
    assert st["last_agent_text"] == "the answer is 42"
