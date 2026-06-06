"""Fix 2: codex render/audit parser + diagnostics + turn reassembly."""
from __future__ import annotations

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.engine.replay import extract_diagnostics, parse_message


def test_parse_codex_text_only_message() -> None:
    raw = {"content": [{"type": "text", "text": "hello"}]}
    msg = parse_message("codex", raw)
    assert msg.role == "assistant"
    assert msg.content == (TextBlock(text="hello"),)


def test_parse_codex_message_with_tool_use_block() -> None:
    raw = {"content": [
        {"type": "text", "text": "calling tool"},
        {"type": "tool_use", "id": "call-1", "name": "edit_file",
         "input": {"path": "a.py"}},
    ]}
    msg = parse_message("codex", raw)
    assert msg.content == (
        TextBlock(text="calling tool"),
        ToolUseBlock(id="call-1", name="edit_file", input={"path": "a.py"}),
    )


def test_parse_codex_web_search_block_is_query_only() -> None:
    # web_search is represented as a tool_use carrying only the query (the
    # ungoverned/opaque limitation: codex exposes no results/citations).
    raw = {"content": [
        {"type": "tool_use", "id": "ws-1", "name": "web_search",
         "input": {"query": "python asyncio"}},
    ]}
    msg = parse_message("codex", raw)
    assert msg.content == (
        ToolUseBlock(id="ws-1", name="web_search",
                     input={"query": "python asyncio"}),
    )


def test_codex_diagnostics_empty_for_known_blocks() -> None:
    raw = {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "c1", "name": "edit_file", "input": {}},
    ]}
    diag = extract_diagnostics("codex", raw)
    assert diag.block_warnings == ()
    assert diag.thinkings == ()


def test_codex_diagnostics_warns_on_unknown_block() -> None:
    raw = {"content": [{"type": "mystery_block", "data": 1}]}
    diag = extract_diagnostics("codex", raw)
    assert any("mystery_block" in w for w in diag.block_warnings)
