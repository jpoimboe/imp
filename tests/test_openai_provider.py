"""OpenAI provider helpers — message translation and tool-schema wrapping.

The runner-level tests live in tests/test_openai_runner.py. This file only
exercises the pure functions (no fake client needed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import (
    _imp_to_responses_input,
    _parse_responses_message,
    _responses_tool_schema,
)
from tests.openai_fakes import resp_function_call, resp_response


def _make_httpx_resp(status_code: int = 400, headers: dict | None = None) -> object:
    """Return a minimal duck-typed response object the OpenAI SDK accepts."""
    import httpx
    h = httpx.Headers(headers or {})

    class _Resp:
        request = httpx.Request("GET", "http://example.com")

    r = _Resp()
    r.status_code = status_code  # type: ignore[attr-defined]
    r.headers = h  # type: ignore[attr-defined]
    return r


def _agent() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(),
        provider_name="openai", model_name="gpt-4",
        system_prompt="be terse", initial_messages=(),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )


def test_imp_to_responses_system_skipped_user_kept() -> None:
    # The system prompt rides on the `instructions` param, so system messages
    # are dropped from input[]; plain user text becomes an input_text message.
    out = _imp_to_responses_input([
        Message(role="system", content=(TextBlock(text="be terse"),)),
        Message(role="user", content=(TextBlock(text="hi"),)),
    ])
    assert out == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]},
    ]


def test_imp_to_responses_assistant_with_tool_uses() -> None:
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="calling complete_step"),
            ToolUseBlock(id="c1", name="complete_step", input={"status": "Success"}),
        ),
    )
    out = _imp_to_responses_input([msg])
    # One assistant output_text message, then a function_call item.
    assert out[0] == {"type": "message", "role": "assistant",
                      "content": [{"type": "output_text",
                                   "text": "calling complete_step"}]}
    fc = out[1]
    assert fc["type"] == "function_call"
    assert fc["call_id"] == "c1"
    assert fc["name"] == "complete_step"
    assert json.loads(fc["arguments"]) == {"status": "Success"}


def test_imp_to_responses_user_with_tool_results_fans_out() -> None:
    msg = Message(
        role="user",
        content=(
            ToolResultBlock(tool_use_id="c1", content="ok"),
            ToolResultBlock(tool_use_id="c2", content="bad", is_error=True),
        ),
    )
    out = _imp_to_responses_input([msg])
    assert len(out) == 2
    assert out[0] == {"type": "function_call_output",
                      "call_id": "c1", "output": "ok"}
    # is_error → embedded prefix because Responses has no flag for it.
    assert out[1]["type"] == "function_call_output"
    assert out[1]["call_id"] == "c2"
    assert out[1]["output"].startswith("[error]")
    assert "bad" in out[1]["output"]


def test_imp_to_responses_text_co_riding_with_tool_results_is_kept() -> None:
    # On resume, balance_success_on_resume builds a single user message that
    # carries the synthesized complete_step tool_result AND the reviewer/gate
    # feedback (plus the resume-durability note) as co-riding TextBlocks. The
    # function_call_output(s) must still be emitted, but the trailing text must
    # NOT be dropped -- it has to reach the model as a follow-on user message.
    msg = Message(
        role="user",
        content=(
            ToolResultBlock(tool_use_id="c1", content="previous output recorded"),
            TextBlock(text="GATE FAILED: re-run the linter"),
        ),
    )
    out = _imp_to_responses_input([msg])
    assert out[0] == {"type": "function_call_output",
                      "call_id": "c1", "output": "previous output recorded"}
    assert out[-1] == {"type": "message", "role": "user",
                       "content": [{"type": "input_text",
                                    "text": "GATE FAILED: re-run the linter"}]}


def test_imp_to_responses_tool_results_without_text_emit_no_message() -> None:
    # Guard the fix: a tool-result-only user message must NOT gain a spurious
    # empty input_text message.
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="c1", content="ok"),),
    )
    out = _imp_to_responses_input([msg])
    assert out == [{"type": "function_call_output", "call_id": "c1", "output": "ok"}]


def test_responses_tool_schema_is_flat() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="note", type="string", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    tool = make_complete_step_tool(schema)
    wrapped = _responses_tool_schema(tool)
    assert wrapped["type"] == "function"
    assert wrapped["name"] == "complete_step"
    assert "function" not in wrapped          # flat, no nested wrapper
    assert wrapped["strict"] is True
    # Strict-mode discipline: optionals are nullable and in required.
    params = wrapped["parameters"]
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
    """Build an imp message list, convert to Responses input[], parse a canned
    Responses output, then verify the resulting imp Message ends the
    conversation correctly."""
    # Imp-side: user said "go".
    user = Message(role="user", content=(TextBlock(text="go"),))
    out = _imp_to_responses_input([user])
    assert out == [{"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "go"}]}]

    raw = resp_response(
        resp_function_call("c1", "complete_step", '{"status": "Success"}'),
    ).model_dump(mode="json")
    parsed = _parse_responses_message(raw)
    tool_uses = [b for b in parsed.content if isinstance(b, ToolUseBlock)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "complete_step"
    assert tool_uses[0].input == {"status": "Success"}


def test_classify_openai_exception_auth() -> None:
    """AuthenticationError → AuthError(category='auth')."""
    import openai as sdk

    from imp.providers import errors as perr
    from imp.providers import openai as op

    resp = _make_httpx_resp()
    exc = sdk.AuthenticationError(
        message="Invalid API key", response=resp, body=None,
    )
    out = op._classify_openai_exception(exc)
    assert isinstance(out, perr.AuthError)
    assert out.category == "auth"


def test_classify_openai_exception_rate_limit_with_retry_after() -> None:
    """RateLimitError → RateLimitError(detail.retry_after_seconds)."""
    import openai as sdk

    from imp.providers import errors as perr
    from imp.providers import openai as op

    resp = _make_httpx_resp(headers={"retry-after": "7"})
    exc = sdk.RateLimitError(message="429", response=resp, body=None)
    out = op._classify_openai_exception(exc)
    assert isinstance(out, perr.RateLimitError)
    assert out.detail.get("retry_after_seconds") == 7.0


def test_classify_openai_exception_context_window() -> None:
    """BadRequestError containing 'maximum context length' → ContextWindowError."""
    import openai as sdk

    from imp.providers import errors as perr
    from imp.providers import openai as op

    resp = _make_httpx_resp()
    exc = sdk.BadRequestError(
        message="This model's maximum context length is 128000 tokens",
        response=resp, body=None,
    )
    out = op._classify_openai_exception(exc)
    assert isinstance(out, perr.ContextWindowError)


def test_classify_openai_exception_quota() -> None:
    """BadRequestError containing 'insufficient_quota' → QuotaError."""
    import openai as sdk

    from imp.providers import errors as perr
    from imp.providers import openai as op

    resp = _make_httpx_resp()
    exc = sdk.BadRequestError(
        message="You exceeded your current quota (insufficient_quota)",
        response=resp, body=None,
    )
    out = op._classify_openai_exception(exc)
    assert isinstance(out, perr.QuotaError)


def test_classify_openai_exception_network() -> None:
    """APIConnectionError → NetworkError."""
    import openai as sdk

    from imp.providers import errors as perr
    from imp.providers import openai as op

    exc = sdk.APIConnectionError(request=None)
    out = op._classify_openai_exception(exc)
    assert isinstance(out, perr.NetworkError)


def test_classify_openai_exception_unknown_reraises() -> None:
    """RuntimeError (unrelated) → re-raised so top-level fallback handles it."""
    from imp.providers import openai as op
    with pytest.raises(RuntimeError):
        op._classify_openai_exception(RuntimeError("kaboom"))


@pytest.mark.anyio
async def test_runner_translates_sdk_auth_error_to_event(tmp_path: Path) -> None:
    """When the SDK raises AuthenticationError mid-stream, the runner
    must yield a single category=auth error event and return."""
    from openai import AuthenticationError

    from imp.providers.openai import OpenAIAgentRunner

    class _ExplodingResponses:
        async def create(self, **_kwargs):
            raise AuthenticationError(
                message="Invalid API key",
                response=_make_httpx_resp(),
                body=None,
            )

    class _ExplodingClient:
        responses = _ExplodingResponses()

        async def close(self):
            return None

    runner = OpenAIAgentRunner(
        model_name="gpt-4", client_factory=lambda: _ExplodingClient(),
    )
    (tmp_path / "step").mkdir()
    inv = _inv(tmp_path)

    events: list[dict] = []
    async for ev in runner.run(inv):
        events.append(ev)

    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    payload = error_events[0]["payload"]
    assert payload["category"] == "auth"


def test_real_sdk_accepts_responses_params() -> None:
    """Guard: the kwarg-capture tests use a fake that swallows **kwargs.
    This pins the real openai SDK contract our runner depends on — the
    Responses create() must accept the params the flipped runner passes."""
    import inspect

    from openai.resources.responses import AsyncResponses

    params = inspect.signature(AsyncResponses.create).parameters
    for name in ("model", "input", "instructions", "tools", "store",
                 "prompt_cache_key"):
        assert name in params, f"responses.create missing {name!r}"


@pytest.fixture
def anyio_backend():
    return "asyncio"
