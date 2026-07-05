from __future__ import annotations

from imp.agents.runner import (
    ImagePart,
    Message,
    TextBlock,
    TextPart,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.tools import Tool
from imp.providers.openai import (
    _extract_responses_diagnostics,
    _imp_to_responses_input,
    _parse_responses_message,
    _responses_tool_schema,
    _serialize_responses_response,
)
from tests.openai_fakes import (
    FakeResponsesClient,
    resp_function_call,
    resp_message,
    resp_response,
    resp_web_search_call,
)


def test_fake_responses_client_records_calls_and_returns() -> None:
    canned = resp_response(resp_message("hi"))
    client = FakeResponsesClient([canned])
    # Exercise the SDK surface the runner will use.
    import anyio

    async def go():
        async with client.responses.stream(
            model="m", input=[], instructions="s"
        ) as stream:
            async for _ in stream:
                pass
            return await stream.get_final_response()

    out = anyio.run(go)
    assert out is canned
    assert client.responses.calls[0]["instructions"] == "s"
    dumped = out.model_dump(mode="json")
    assert dumped["output"][0]["type"] == "message"


def test_response_with_function_call_and_web_search_dumps() -> None:
    r = resp_response(
        resp_web_search_call("ws1", query="imp"),
        resp_function_call("c1", "bash", '{"cmd":"ls"}'),
        resp_message("done", citations=[{"url": "https://a.test", "title": "A"}]),
    )
    d = r.model_dump(mode="json")
    types = [item["type"] for item in d["output"]]
    assert types == ["web_search_call", "function_call", "message"]


def _tool() -> Tool:
    return Tool(name="bash", description="run",
                input_schema={"type": "object",
                              "properties": {"cmd": {"type": "string"}},
                              "required": ["cmd"]},
                fn=lambda ctx, cmd: cmd)


def test_responses_tool_schema_is_flat_function() -> None:
    s = _responses_tool_schema(_tool())
    assert s["type"] == "function"
    assert s["name"] == "bash"
    assert "function" not in s          # no nested wrapper
    assert s["strict"] is True
    assert "properties" in s["parameters"]


def test_responses_input_conversion_items() -> None:
    messages = [
        Message(role="assistant", content=(
            TextBlock(text="ok"),
            ToolUseBlock(id="c1", name="bash", input={"cmd": "ls"}))),
        Message(role="user", content=(ToolResultBlock("c1", "out"),)),
        Message(role="user", content=(TextBlock(text="next"),)),
    ]
    items = _imp_to_responses_input(messages)
    types = [it["type"] for it in items]
    assert "function_call" in types
    assert "function_call_output" in types
    fc = next(it for it in items if it["type"] == "function_call")
    assert fc["call_id"] == "c1" and fc["name"] == "bash"
    fco = next(it for it in items if it["type"] == "function_call_output")
    assert fco["call_id"] == "c1" and fco["output"] == "out"


def test_serialize_responses_prefers_model_dump() -> None:
    r = resp_response(resp_message("hello"))
    raw = _serialize_responses_response(r)
    assert raw["output"][0]["type"] == "message"


def test_parse_responses_text_and_function_call() -> None:
    raw = resp_response(
        resp_message("answer"),
        resp_function_call("c1", "bash", '{"cmd":"ls"}'),
    ).model_dump(mode="json")
    msg = _parse_responses_message(raw)
    texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
    tus = [b for b in msg.content if isinstance(b, ToolUseBlock)]
    assert texts == ["answer"]
    assert tus[0].id == "c1" and tus[0].name == "bash" and tus[0].input == {"cmd": "ls"}


def test_parse_responses_ignores_web_search_call_item() -> None:
    raw = resp_response(
        resp_web_search_call("ws1", query="q"),
        resp_message("done"),
    ).model_dump(mode="json")
    msg = _parse_responses_message(raw)
    # web_search_call is a server-side op; never a dispatchable ToolUseBlock.
    assert not any(isinstance(b, ToolUseBlock) for b in msg.content)


def test_responses_diagnostics_no_warn_on_web_search_call() -> None:
    raw = resp_response(resp_web_search_call("ws1", query="q"),
                        resp_message("ok")).model_dump(mode="json")
    assert _extract_responses_diagnostics(raw).block_warnings == ()


def test_responses_diagnostics_warn_on_bad_function_args() -> None:
    raw = resp_response(
        resp_function_call("c1", "bash", "{not json}")).model_dump(mode="json")
    warns = _extract_responses_diagnostics(raw).block_warnings
    assert any("not JSON" in w for w in warns)


def test_responses_input_list_content_emits_input_image() -> None:
    parts = [
        TextPart(text="shot.png — 64×48 JPEG"),
        ImagePart(media_type="image/jpeg", data="aGVsbG8=", width=64,
                  height=48, blob="cd34.jpg", nbytes=5),
    ]
    msg = Message(role="user",
                  content=(ToolResultBlock(tool_use_id="c1", content=parts),))
    out = _imp_to_responses_input([msg])
    fco = [it for it in out if it["type"] == "function_call_output"]
    assert len(fco) == 1
    assert fco[0]["call_id"] == "c1"
    output = fco[0]["output"]
    assert isinstance(output, list)
    assert output[0] == {"type": "input_text", "text": "shot.png — 64×48 JPEG"}
    assert output[1] == {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64,aGVsbG8=",
        "detail": "auto",
    }


def test_responses_input_str_content_unchanged() -> None:
    msg = Message(role="user",
                  content=(ToolResultBlock(tool_use_id="c1", content="boom",
                                           is_error=True),))
    out = _imp_to_responses_input([msg])
    fco = [it for it in out if it["type"] == "function_call_output"]
    # Bare-str content stays a string; the [error] prefix is unchanged.
    assert fco[0]["output"] == "[error] boom"
