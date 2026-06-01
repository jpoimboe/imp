from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicProvider


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
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse", initial_messages=(),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )


def test_provider_name_and_model() -> None:
    p = AnthropicProvider(model_name="claude-sonnet-4-6")
    assert p.name == "anthropic"
    assert p.model == "claude-sonnet-4-6"  # public; engine uses this


def test_create_runner_returns_runner(tmp_path: Path) -> None:
    p = AnthropicProvider(model_name="claude-sonnet-4-6")
    runner = p.create_runner(_inv(tmp_path))
    assert hasattr(runner, "run")
    assert hasattr(runner, "interrupt")


def _make_httpx_resp(status_code: int = 400, headers: dict | None = None) -> object:
    """Return a minimal duck-typed response object the Anthropic SDK accepts."""
    import httpx
    h = httpx.Headers(headers or {})

    class _Resp:
        request = httpx.Request("GET", "http://example.com")

    r = _Resp()
    r.status_code = status_code  # type: ignore[attr-defined]
    r.headers = h  # type: ignore[attr-defined]
    return r


def test_classify_anthropic_exception_auth() -> None:
    """AuthenticationError → AuthError(category='auth')."""
    import anthropic as sdk

    from imp.providers import anthropic as ap
    from imp.providers import errors as perr

    exc = sdk.AuthenticationError(
        message="Invalid API key",
        response=_make_httpx_resp(401),  # type: ignore[arg-type]
        body=None,
    )
    out = ap._classify_anthropic_exception(exc)
    assert isinstance(out, perr.AuthError)
    assert out.category == "auth"


def test_classify_anthropic_exception_rate_limit_with_retry_after() -> None:
    """RateLimitError → RateLimitError(category='rate_limit', detail.retry_after_seconds)."""
    import anthropic as sdk

    from imp.providers import anthropic as ap
    from imp.providers import errors as perr

    exc = sdk.RateLimitError(
        message="429",
        response=_make_httpx_resp(429, {"retry-after": "12.5"}),  # type: ignore[arg-type]
        body=None,
    )
    out = ap._classify_anthropic_exception(exc)
    assert isinstance(out, perr.RateLimitError)
    assert out.detail.get("retry_after_seconds") == 12.5


def test_classify_anthropic_exception_context_window() -> None:
    """BadRequestError containing 'prompt is too long' → ContextWindowError."""
    import anthropic as sdk

    from imp.providers import anthropic as ap
    from imp.providers import errors as perr

    exc = sdk.BadRequestError(
        message="prompt is too long: 250000 tokens",
        response=_make_httpx_resp(400),  # type: ignore[arg-type]
        body=None,
    )
    out = ap._classify_anthropic_exception(exc)
    assert isinstance(out, perr.ContextWindowError)


def test_classify_anthropic_exception_quota() -> None:
    """BadRequestError containing 'credit balance' → QuotaError."""
    import anthropic as sdk

    from imp.providers import anthropic as ap
    from imp.providers import errors as perr

    exc = sdk.BadRequestError(
        message="Your credit balance is too low",
        response=_make_httpx_resp(400),  # type: ignore[arg-type]
        body=None,
    )
    out = ap._classify_anthropic_exception(exc)
    assert isinstance(out, perr.QuotaError)


def test_classify_anthropic_exception_network() -> None:
    """APIConnectionError → NetworkError."""
    import anthropic as sdk

    from imp.providers import anthropic as ap
    from imp.providers import errors as perr

    exc = sdk.APIConnectionError(request=None)
    out = ap._classify_anthropic_exception(exc)
    assert isinstance(out, perr.NetworkError)


def test_classify_anthropic_exception_unknown_reraises() -> None:
    """RuntimeError (unrelated) → re-raised so the top-level fallback
    handles it as 'unknown'."""
    from imp.providers import anthropic as ap

    with pytest.raises(RuntimeError):
        ap._classify_anthropic_exception(RuntimeError("kaboom"))


@pytest.mark.anyio
async def test_runner_translates_sdk_auth_error_to_event(tmp_path: Path) -> None:
    """When the SDK raises AuthenticationError mid-stream, the runner
    must yield a single category=auth error event and return."""
    from anthropic import AuthenticationError

    from imp.providers.anthropic import AnthropicAgentRunner

    class _ExplodingMessages:
        async def create(self, **_kwargs):
            raise AuthenticationError(
                message="Invalid API key",
                response=_make_httpx_resp(401),  # type: ignore[arg-type]
                body=None,
            )

    class _ExplodingClient:
        messages = _ExplodingMessages()

        async def close(self):
            return None

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: _ExplodingClient(),
    )
    (tmp_path / "step").mkdir()
    inv = _inv(tmp_path)

    events: list[dict] = []
    async for ev in runner.run(inv):
        events.append(ev)

    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1, f"expected 1 error event, got {events}"
    payload = error_events[0]["payload"]
    assert payload["category"] == "auth"
    assert "Invalid API key" in payload["message"]


def test_real_sdk_accepts_cache_control() -> None:
    """Guard: the kwarg-capture test uses a fake that swallows **kwargs.
    This pins the real anthropic SDK contract our runner depends on."""
    import inspect

    from anthropic.resources.messages import AsyncMessages

    params = inspect.signature(AsyncMessages.create).parameters
    assert "cache_control" in params


@pytest.fixture
def anyio_backend():
    return "asyncio"
