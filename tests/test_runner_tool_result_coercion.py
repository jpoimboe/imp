"""Fix F: ToolResultBlock coerces is_error to a real bool (no JSON null)."""

from __future__ import annotations

import json

from imp.agents.runner import (
    ToolResultBlock,
    _block_to_dict,
    message_from_json,
    message_to_json,
)


def test_none_is_error_coerced_to_false() -> None:
    b = ToolResultBlock(tool_use_id="t1", content="ok", is_error=None)  # type: ignore[arg-type]
    assert b.is_error is False


def test_block_to_dict_emits_bool_not_null() -> None:
    b = ToolResultBlock(tool_use_id="t1", content="ok", is_error=None)  # type: ignore[arg-type]
    d = _block_to_dict(b)
    assert d["is_error"] is False
    assert isinstance(d["is_error"], bool)


def test_persisted_null_round_trips_to_false() -> None:
    # Simulate a transcript line whose tool_result carries JSON null.
    raw = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "ok", "is_error": None},
        ],
    }
    msg = message_from_json(raw)
    block = msg.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.is_error is False
    # Re-serialize: the emitted dict must carry a bool, not null.
    reparsed = message_from_json(
        {"role": "user", "content": [json.loads(message_to_json(msg))["content"][0]]}
    )
    assert reparsed.content[0].is_error is False  # type: ignore[union-attr]


def test_state_serializer_emits_bool_not_null() -> None:
    # Spec Fix F requires the state.py serializer path also be proven: an
    # append_tool_result round-trip must emit a bool, never null.
    from imp.engine.state import InMemoryEventLog

    log = InMemoryEventLog()
    rec = log.append_tool_result(
        agent_id="a",
        block=ToolResultBlock(tool_use_id="t1", content="ok", is_error=None),  # type: ignore[arg-type]
    )
    # state.py serializes the tool_result error flag under the "error" key.
    assert rec["payload"]["error"] is False
    assert isinstance(rec["payload"]["error"], bool)
