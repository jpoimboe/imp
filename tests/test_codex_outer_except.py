from __future__ import annotations

import pytest

from imp.engine.agent_run import SubagentSuspended
from imp.providers.codex_runner import CodexAgentRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _runner_with_failing_loop(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException,
) -> CodexAgentRunner:
    runner = CodexAgentRunner(model_name="gpt-5.5")

    async def _boom(invocation, session):  # matches run_agent_loop(invocation, session)
        if False:
            yield {}
        raise exc

    monkeypatch.setattr("imp.providers.codex_runner.run_agent_loop", _boom)
    return runner


@pytest.mark.anyio
async def test_outer_except_reclassifies_to_interrupted_when_cancel_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_with_failing_loop(monkeypatch, RuntimeError("transport torn down"))
    runner._cancel = True

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0] == {"type": "error", "payload": {
        "category": "config", "kind": "interrupted",
        "message": "runner cancelled by interrupt"}}


@pytest.mark.anyio
async def test_outer_except_unknown_when_cancel_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_with_failing_loop(monkeypatch, RuntimeError("genuine bug"))
    runner._cancel = False

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["payload"]["category"] == "unknown"


@pytest.mark.anyio
async def test_codex_event_parse_error_yields_a_config_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodexEventParseError is a ProviderError, so the existing catch classifies it
    with no new plumbing."""
    from imp.providers.codex_events import CodexEventParseError

    message = (
        "codex event parse failure: item type 'mcp_tool_call' does not match imp's "
        "codex exec event model — update src/imp/providers/codex_events.py")
    runner = _runner_with_failing_loop(monkeypatch, CodexEventParseError(
        message, detail={"item_type": "mcp_tool_call", "validation_error": "boom"}))

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0] == {"type": "error", "payload": {
        "category": "config", "kind": "CodexEventParseError",
        "message": message,
        "detail": {"item_type": "mcp_tool_call", "validation_error": "boom"}}}


@pytest.mark.anyio
async def test_outer_except_reraises_subagent_suspended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_with_failing_loop(monkeypatch, SubagentSuspended("paused"))
    runner._cancel = False

    with pytest.raises(SubagentSuspended):
        async for _ev in runner.run(object()):  # type: ignore[arg-type]
            pass


@pytest.mark.anyio
async def test_outer_except_reraises_awaiting_user_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    runner = _runner_with_failing_loop(monkeypatch, AwaitingUserInput("the question"))
    runner._cancel = False

    with pytest.raises(AwaitingUserInput):
        async for _ev in runner.run(object()):  # type: ignore[arg-type]
            pass
