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
    _imp_to_openai_messages,
    _openai_message_to_imp,
    _openai_tool_schema,
)
from tests.openai_fakes import FakeFunction, FakeMessage, FakeToolCall


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
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )


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

    class _ExplodingCompletions:
        async def create(self, **_kwargs):
            raise AuthenticationError(
                message="Invalid API key",
                response=_make_httpx_resp(),
                body=None,
            )

    class _ExplodingChat:
        completions = _ExplodingCompletions()

    class _ExplodingClient:
        chat = _ExplodingChat()

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


def test_real_sdk_accepts_prompt_cache_key() -> None:
    """Guard: the kwarg-capture tests use a fake that swallows **kwargs.
    This pins the real openai SDK contract our runner depends on."""
    import inspect

    from openai.resources.chat.completions import AsyncCompletions

    params = inspect.signature(AsyncCompletions.create).parameters
    assert "prompt_cache_key" in params


@pytest.fixture
def anyio_backend():
    return "asyncio"
