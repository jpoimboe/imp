# tests/test_runner_types.py
from __future__ import annotations

import json
from pathlib import Path

from imp.agents.runner import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    message_from_json,
    message_to_json,
)


def test_message_immutable() -> None:
    msg = Message(role="user", content=(TextBlock(text="hi"),))
    assert msg.role == "user"
    assert msg.content[0].text == "hi"
    try:
        msg.role = "assistant"  # type: ignore[misc]
        raise AssertionError("Message should be frozen")
    except AttributeError:
        pass


def test_round_trip_text_only() -> None:
    msg = Message(role="assistant", content=(TextBlock(text="hello"),))
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    assert back == msg
    assert isinstance(back.content, tuple)  # JSON list → tuple


def test_round_trip_tool_use_and_result() -> None:
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="reading…"),
            ToolUseBlock(id="t1", name="read_file", input={"path": "/p/x"}),
        ),
    )
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    assert back == msg

    result = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content="contents", is_error=False),),
    )
    raw = message_to_json(result)
    back = message_from_json(json.loads(raw))
    assert back == result


def test_agent_invocation_audit_fields_default_safely(tmp_path) -> None:
    from imp.agents.runner import (
        AgentInvocation, ChangeContext, Message, TextBlock,
    )
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    inv = AgentInvocation(
        agent_def=AgentDef(
            name="t", description="", tools=frozenset({"complete_step"}),
            subagents=(),
            output_schema=OutputSchema(
                fields=(OutputField(name="status", type="enum",
                                    values=("Success",), required=True),),
                source=SourceLoc(file=Path("a.md"), line=1)),
            prompt_template_source="hi", source_path=Path("a.md"),
            source=SourceLoc(file=Path("a.md"), line=1)),
        provider_name="anthropic", model_name="m", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="x"),)),),
        change_context=ChangeContext(change_id=1, project_root=tmp_path,
                                     artifacts_dir=tmp_path),
        interactive=False, transcript_dir=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    assert inv.agent_id == ""
    assert inv.event_log is None
    assert inv.first_event_parent_id is None


def test_diagnostics_holds_warnings_and_thinkings() -> None:
    from imp.agents.runner import Diagnostics
    d = Diagnostics(block_warnings=("w1",), thinkings=("t1", "t2"))
    assert d.block_warnings == ("w1",)
    assert d.thinkings == ("t1", "t2")
