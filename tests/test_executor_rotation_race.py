"""Scope 5: a token_rotated 401 retries at the ladder's bottom rung (capped at
three such sleeps) without disturbing the ladder counter, and every agent_retry
carries the error's kind when it has one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.engine.executor import RunResult, run_quest
from tests.test_executor_retry_continue import _SINGLE_WF, _WRITER, _make_quest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _ScriptRunner:
    """One drive: the scripted error event, or a success that completes the step."""

    def __init__(self, error: dict[str, Any] | None) -> None:
        self._error = error

    async def run(self, invocation: Any) -> Any:
        if self._error is not None:
            yield {"type": "error", "payload": self._error}
        else:
            yield {"type": "agent_result",
                   "payload": {"status": "Success", "outcome": "done"}}

    async def interrupt(self) -> None:
        pass


class _ScriptProvider:
    """One script entry per drive; past the end of the script every drive succeeds."""

    name = "fake"
    model = "m"

    def __init__(self, script: list[dict[str, Any] | None]) -> None:
        self._script = script
        self.drives = 0

    def create_runner(self, _inv: Any) -> _ScriptRunner:
        i = self.drives
        self.drives += 1
        return _ScriptRunner(self._script[i] if i < len(self._script) else None)


def _rotated() -> dict[str, Any]:
    """The token_rotated auth error, in the shape the provider emits."""
    return {"category": "auth", "kind": "token_rotated",
            "message": "claude CLI: Failed to authenticate. API Error: 401 "
                       "OAuth access token has been revoked.",
            "detail": {}}


async def _no_sleep(_delay: float, _coord: Any) -> None:
    return None


def _retries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["payload"] for e in events if e["type"] == "agent_retry"]


@pytest.mark.anyio
async def test_agent_retry_payload_carries_the_error_kind(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer needs the kind to tell a race from an ordinary 401, and only
    the executor can put it on the payload."""
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _ScriptProvider([_rotated()])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    retries = _retries(events)
    assert len(retries) == 1
    assert retries[0]["kind"] == "token_rotated"
    assert retries[0]["category"] == "auth"
    assert retries[0]["attempt"] == 1


@pytest.mark.anyio
async def test_agent_retry_omits_kind_when_the_error_carries_none(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent kind means no key, exactly as `message` behaves today; a plain
    network blip also keeps its full ladder."""
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    blip = {"category": "network", "message": "connection reset", "detail": {}}
    provider = _ScriptProvider([dict(blip), dict(blip)])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    retries = _retries(events)
    assert [p["backoff_seconds"] for p in retries] == [1.0, 2.0]
    assert all("kind" not in p for p in retries)
    assert all("message" in p for p in retries)
