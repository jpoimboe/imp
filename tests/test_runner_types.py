# tests/test_runner_types.py
from __future__ import annotations

import base64
import json
from pathlib import Path

from imp.agents.runner import (
    ImagePart,
    Message,
    TextBlock,
    TextPart,
    ThinkingBlock,
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
        AgentInvocation,
        Message,
        QuestContext,
        TextBlock,
    )
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
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
        quest_context=QuestContext(title="Test", project_root=tmp_path,
                                     workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    assert inv.agent_id == ""
    assert inv.event_log is None
    assert inv.first_event_parent_id is None


def test_agent_invocation_has_continuation_fn_default_none() -> None:
    from pathlib import Path

    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

    schema = OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    agent_def = AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(), output_schema=schema, prompt_template_source="hi",
        source_path=Path("a.md"), source=SourceLoc(file=Path("a.md"), line=1),
    )
    inv = AgentInvocation(
        agent_def=agent_def, provider_name="anthropic", model_name="m",
        system_prompt="x",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="Test", project_root=Path("."),
                                     workflow_dir=Path(".") / ".imp" / "change" / "workflow"),
        transcript_dir=Path("."),
events_path=Path("e.jsonl"),
    )
    assert inv.continuation_fn is None

    captured: list[str] = []
    inv2 = AgentInvocation(
        agent_def=agent_def, provider_name="anthropic", model_name="m",
        system_prompt="x",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="Test", project_root=Path("."),
                                     workflow_dir=Path(".") / ".imp" / "change" / "workflow"),
        transcript_dir=Path("."),
events_path=Path("e.jsonl"),
        continuation_fn=lambda last: captured.append(last) or "nudge",
    )
    assert inv2.continuation_fn("prose") == "nudge"
    assert captured == ["prose"]


def test_diagnostics_holds_warnings_and_thinkings() -> None:
    from imp.agents.runner import Diagnostics
    d = Diagnostics(block_warnings=("w1",), thinkings=("t1", "t2"))
    assert d.block_warnings == ("w1",)
    assert d.thinkings == ("t1", "t2")


def test_agent_invocation_has_tiers_field() -> None:
    import dataclasses

    from imp.agents.runner import AgentInvocation
    fields = {f.name: f for f in dataclasses.fields(AgentInvocation)}
    assert "tiers" in fields
    assert fields["tiers"].default == ()
    assert "resolved_tiers" in fields
    assert "model_role" not in fields
    assert "model_tiers" not in fields


def test_agent_invocation_resolved_tiers_defaults_none():
    from pathlib import Path

    from imp.agents.runner import AgentInvocation, QuestContext
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    a = AgentDef(
        name="x", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    inv = AgentInvocation(
        agent_def=a, provider_name="p", model_name="m", system_prompt="",
        initial_messages=(),
        quest_context=QuestContext(title="x", project_root=Path("."),
                                     workflow_dir=Path(".") / ".imp" / "change" / "workflow"),
        transcript_dir=Path("."),
        events_path=Path("e.jsonl"))
    assert inv.resolved_tiers is None


def test_round_trip_thinking_block() -> None:
    msg = Message(role="assistant", content=(
        ThinkingBlock(thinking="reasoning", signature="sig-abc"),
        TextBlock(text="answer"),
    ))
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    assert back == msg
    assert back.content[0].signature == "sig-abc"
    assert back.content[0].redacted_thinking is None


def test_round_trip_redacted_thinking_block() -> None:
    msg = Message(role="assistant", content=(
        ThinkingBlock(redacted_thinking="encrypted-data"),
        TextBlock(text="answer"),
    ))
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    assert back == msg
    assert back.content[0].redacted_thinking == "encrypted-data"
    assert back.content[0].thinking == ""


def test_round_trip_tool_result_image_rebuilds_from_blob(tmp_path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "deadbeef.png").write_bytes(b"hello")  # sha-named blob on disk
    parts = [
        TextPart(text="login.png — 1200×800 PNG"),
        ImagePart(
            media_type="image/png",
            data=base64.b64encode(b"hello").decode("ascii"),
            width=1200, height=800, blob="deadbeef.png", nbytes=5,
        ),
    ]
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content=parts),),
    )
    raw = message_to_json(msg)
    # The persisted transcript carries a blob reference, NOT the base64 payload.
    assert '"data"' not in raw
    assert "deadbeef.png" in raw

    back = message_from_json(json.loads(raw), blobs_dir=blobs)
    block = back.content[0]
    assert isinstance(block, ToolResultBlock)
    assert isinstance(block.content, list)
    # data rebuilt from the blob bytes -> an ImagePart identical to the original.
    assert block.content == parts
    img = block.content[1]
    assert isinstance(img, ImagePart)
    assert base64.b64decode(img.data) == b"hello"


def test_round_trip_tool_result_missing_blob_degrades_to_text_note(tmp_path) -> None:
    parts = [
        ImagePart(
            media_type="image/png",
            data=base64.b64encode(b"hi").decode("ascii"),
            width=10, height=10, blob="cafef00d.png", nbytes=2,
        ),
    ]
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content=parts),),
    )
    # blobs dir exists but the blob file is absent -> degrade to a text note.
    back = message_from_json(json.loads(message_to_json(msg)), blobs_dir=tmp_path)
    note = back.content[0].content[0]
    assert isinstance(note, TextPart)
    assert note.text == "[image cafef00d no longer available]"


def test_round_trip_tool_result_str_content_unchanged() -> None:
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content="contents", is_error=True),),
    )
    back = message_from_json(json.loads(message_to_json(msg)))
    block = back.content[0]
    assert block.content == "contents"
    assert isinstance(block.content, str)
    assert block.is_error is True


def test_round_trip_native_list_dict_tool_result_verbatim() -> None:
    native = [{"type": "text", "text": "hi"}]
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content=native),),
    )
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    block = back.content[0]
    # Content must survive the round-trip unchanged as a list of plain dicts.
    assert block.content == native
    assert isinstance(block.content, list)
    assert isinstance(block.content[0], dict)
    # Must NOT have been converted to a TextPart.
    assert not isinstance(block.content[0], TextPart)
