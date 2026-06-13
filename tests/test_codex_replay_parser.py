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


from imp.engine.replay import reconstruct_request_for_response  # noqa: E402
from imp.providers.codex_runner import reassemble_codex_turn  # noqa: E402
from tests.codex_fakes import (  # noqa: E402
    FakeAgentMessageItem,
    FakeMcpToolCallItem,
    FakeWebSearchItem,
)


def test_reassemble_codex_turn_builds_full_turn_raw() -> None:
    items = [
        FakeAgentMessageItem(text="I'll edit the file"),
        FakeMcpToolCallItem(tool="edit_file", arguments={"path": "a.py"},
                            id="call-1"),
        FakeWebSearchItem(query="asyncio gather"),
    ]
    raw = reassemble_codex_turn(items)
    assert raw == {"content": [
        {"type": "text", "text": "I'll edit the file"},
        {"type": "tool_use", "id": "call-1", "name": "edit_file",
         "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "", "name": "web_search",
         "input": {"query": "asyncio gather"}},
    ]}


def test_reassemble_rekeys_task_tool_use_to_minted_id() -> None:
    """AC24: a task tool_use block is re-keyed from its SDK item id to the minted
    event id (from the audit map) so it pairs id-consistently with the live
    tool_result keyed to that minted id; a non-task call keeps its SDK id."""
    items = [
        FakeMcpToolCallItem(tool="task", arguments={"subagent_name": "impl"},
                            id="sdk-task-1"),
        FakeMcpToolCallItem(tool="write_file", arguments={"path": "a.py"},
                            id="sdk-wf-1"),
    ]
    audit = {"sdk-task-1": "MINTED-EVENT-7"}
    raw = reassemble_codex_turn(items, audit=audit)
    blocks = {b["name"]: b["id"] for b in raw["content"]}
    assert blocks["task"] == "MINTED-EVENT-7"  # re-keyed
    assert blocks["write_file"] == "sdk-wf-1"  # untouched


def test_reassemble_without_audit_keeps_sdk_ids() -> None:
    """Back-compat: with no audit map (or an absent entry) task blocks keep their
    SDK item id — the default path the existing reassembly tests exercise."""
    items = [FakeMcpToolCallItem(tool="task", arguments={"subagent_name": "impl"},
                                 id="sdk-task-9")]
    raw = reassemble_codex_turn(items)
    assert raw["content"][0]["id"] == "sdk-task-9"


def test_reconstruct_pairs_codex_tool_use_with_tool_result() -> None:
    """Latent audit-API guard: a codex resumed, tool-calling step reconstructs
    with correctly-paired tool_use/tool_result (no orphans, no KeyError)."""
    turn_items = [
        FakeAgentMessageItem(text="editing"),
        FakeMcpToolCallItem(tool="edit_file", arguments={"path": "a.py"},
                            id="call-1"),
    ]
    raw = reassemble_codex_turn(turn_items)
    events = [
        {"id": "e0", "agent_id": "a", "type": "step_context",
         "payload": {"system_prompt": "sys", "tools_schema": [],
                     "model": "gpt-5.5", "params": {}}},
        {"id": "e1", "agent_id": "a", "type": "llm_response",
         "payload": {"provider": "codex", "raw": raw}},
        {"id": "e2", "agent_id": "a", "type": "tool_result",
         "payload": {"id": "call-1", "content": "edited", "error": False}},
        {"id": "e3", "agent_id": "a", "type": "llm_response",
         "payload": {"provider": "codex",
                     "raw": {"content": [{"type": "text", "text": "done"}]}}},
    ]
    req = reconstruct_request_for_response(events, "e3")
    msgs = req["messages"]
    # assistant turn carries the tool_use; the following user turn carries the
    # matching tool_result with the same id.
    assistant = msgs[0]
    user = msgs[1]
    tool_uses = [b for b in assistant.content if getattr(b, "name", None)]
    assert tool_uses and tool_uses[0].id == "call-1"
    assert user.content[0].tool_use_id == "call-1"
