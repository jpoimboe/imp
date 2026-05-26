"""OpenAI provider helpers — message translation and tool-schema wrapping.

The runner-level tests live in tests/test_openai_runner.py. This file only
exercises the pure functions (no fake client needed).
"""

from __future__ import annotations

import json
from pathlib import Path

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.engine.types import OutputField, OutputSchema, SourceLoc
from imp.providers.openai import (
    _imp_to_openai_messages,
    _openai_message_to_imp,
    _openai_tool_schema,
)
from tests.openai_fakes import FakeFunction, FakeMessage, FakeToolCall


def test_imp_to_openai_system_then_user() -> None:
    out = _imp_to_openai_messages([
        Message(role="system", content=(TextBlock(text="be terse"),)),
        Message(role="user", content=(TextBlock(text="hi"),)),
    ])
    assert out == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]


def test_imp_to_openai_assistant_with_tool_uses() -> None:
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="calling complete_step"),
            ToolUseBlock(id="c1", name="complete_step", input={"status": "Success"}),
        ),
    )
    out = _imp_to_openai_messages([msg])
    assert len(out) == 1
    entry = out[0]
    assert entry["role"] == "assistant"
    assert entry["content"] == "calling complete_step"
    assert entry["tool_calls"][0]["id"] == "c1"
    assert entry["tool_calls"][0]["function"]["name"] == "complete_step"
    assert json.loads(entry["tool_calls"][0]["function"]["arguments"]) == {
        "status": "Success",
    }


def test_imp_to_openai_user_with_tool_results_fans_out() -> None:
    msg = Message(
        role="user",
        content=(
            ToolResultBlock(tool_use_id="c1", content="ok"),
            ToolResultBlock(tool_use_id="c2", content="bad", is_error=True),
        ),
    )
    out = _imp_to_openai_messages([msg])
    assert len(out) == 2
    assert out[0] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    # is_error → embedded prefix because OpenAI has no flag for it.
    assert out[1]["role"] == "tool"
    assert out[1]["tool_call_id"] == "c2"
    assert out[1]["content"].startswith("[error]")
    assert "bad" in out[1]["content"]


def test_openai_message_to_imp_text_and_tool_calls() -> None:
    msg = FakeMessage(
        role="assistant",
        content="thinking…",
        tool_calls=[FakeToolCall(
            id="c1", type="function",
            function=FakeFunction(name="bash", arguments='{"command": "ls"}'),
        )],
    )
    out, warns = _openai_message_to_imp(msg)
    assert out.role == "assistant"
    assert any(isinstance(b, TextBlock) and b.text == "thinking…" for b in out.content)
    assert any(
        isinstance(b, ToolUseBlock) and b.name == "bash" and b.input == {"command": "ls"}
        for b in out.content
    )
    assert warns == []


def test_openai_message_to_imp_refusal_becomes_warning() -> None:
    msg = FakeMessage(role="assistant", content=None, refusal="not allowed")
    out, warns = _openai_message_to_imp(msg)
    assert out.content == ()
    assert any("refusal" in w for w in warns)


def test_openai_message_to_imp_bad_json_args_becomes_warning() -> None:
    msg = FakeMessage(
        role="assistant", content=None,
        tool_calls=[FakeToolCall(
            id="c1", type="function",
            function=FakeFunction(name="bash", arguments="not-json"),
        )],
    )
    out, warns = _openai_message_to_imp(msg)
    # Block is still emitted but with empty input; warning describes the issue.
    blocks = [b for b in out.content if isinstance(b, ToolUseBlock)]
    assert len(blocks) == 1
    assert blocks[0].input == {}
    assert any("not JSON" in w for w in warns)


def test_openai_tool_schema_wraps_with_function() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="note", type="string", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    tool = make_complete_step_tool(schema)
    wrapped = _openai_tool_schema(tool)
    assert wrapped["type"] == "function"
    assert wrapped["function"]["name"] == "complete_step"
    assert wrapped["function"]["strict"] is True
    # Strict-mode discipline: optionals are nullable and in required.
    params = wrapped["function"]["parameters"]
    assert params["additionalProperties"] is False
    assert "note" in params["required"]
    # Pydantic emits Optional[T] as anyOf with a null variant; accept either
    # the anyOf form or the type-widening form.
    note_prop = params["properties"]["note"]
    nullable = (
        (isinstance(note_prop.get("type"), list) and "null" in note_prop["type"])
        or any(
            isinstance(v, dict) and v.get("type") == "null"
            for v in note_prop.get("anyOf", [])
        )
    )
    assert nullable, f"note not nullable: {note_prop}"


def test_round_trip_two_turn_conversation() -> None:
    """Build an imp message list, convert to OpenAI, parse a canned OpenAI
    response, then verify the resulting imp Message ends the conversation
    correctly."""
    # Imp-side: user said "go", assistant called complete_step.
    user = Message(role="user", content=(TextBlock(text="go"),))
    out = _imp_to_openai_messages([user])
    assert out == [{"role": "user", "content": "go"}]

    fake_response = FakeMessage(
        role="assistant", content=None,
        tool_calls=[FakeToolCall(
            id="c1", type="function",
            function=FakeFunction(
                name="complete_step",
                arguments='{"status": "Success"}',
            ),
        )],
    )
    parsed, warns = _openai_message_to_imp(fake_response)
    assert warns == []
    tool_uses = [b for b in parsed.content if isinstance(b, ToolUseBlock)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "complete_step"
    assert tool_uses[0].input == {"status": "Success"}
