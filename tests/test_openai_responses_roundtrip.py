from __future__ import annotations

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
        return await client.responses.create(model="m", input=[], instructions="s")

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
