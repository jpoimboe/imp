from __future__ import annotations

import json

from imp.agents.runner import ImagePart, Message, TextPart, ToolResultBlock
from imp.engine.state import InMemoryEventLog


def _image_block() -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id="t1",
        content=[
            TextPart(text="shot.png — 800×600 PNG"),
            ImagePart(media_type="image/png", data="aGVsbG8=", width=800,
                      height=600, blob="ab12.png", nbytes=1234),
        ],
    )


def test_image_tool_result_persists_lean_reference_no_base64() -> None:
    log = InMemoryEventLog()
    rec = log.append_tool_result(agent_id="step:p/s", block=_image_block())
    content = rec["payload"]["content"]
    assert content[0] == {"type": "text", "text": "shot.png — 800×600 PNG"}
    # Lean blob reference: exactly {blob, media_type, w, h, bytes} + type — no base64.
    assert content[1] == {
        "type": "image", "blob": "ab12.png", "media_type": "image/png",
        "w": 800, "h": 600, "bytes": 1234,
    }
    assert "data" not in content[1]
    # The whole event stays greppable JSON telemetry (no dataclass would serialize).
    json.dumps(rec)


def test_str_tool_result_content_persists_unchanged() -> None:
    log = InMemoryEventLog()
    block = ToolResultBlock(tool_use_id="t2", content="plain text", is_error=True)
    rec = log.append_tool_result(agent_id="step:p/s", block=block)
    assert rec["payload"]["content"] == "plain text"
    assert rec["payload"]["error"] is True


def test_user_message_image_tool_result_serializes_lean() -> None:
    log = InMemoryEventLog()
    msg = Message(role="user", content=(_image_block(),))
    rec = log.append_user_message(agent_id="step:p/s", message=msg)
    part = rec["payload"]["content"][0]["content"][1]
    assert part == {"type": "image", "blob": "ab12.png",
                    "media_type": "image/png", "w": 800, "h": 600, "bytes": 1234}
    assert "data" not in part


def test_block_to_dict_native_list_dict_content_verbatim() -> None:
    from imp.engine.state import _block_to_dict
    native = [{"type": "text", "text": "hi"}]
    block = ToolResultBlock(tool_use_id="t1", content=native)
    result = _block_to_dict(block)
    assert result["content"] == native
