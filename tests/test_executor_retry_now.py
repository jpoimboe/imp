"""Retry-now: the executor backoff loop arms the wait (begin/end_backoff_wait)
around the interruptible sleep and consumes the retry-now latch after it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.engine.executor import RunResult, run_quest
from tests.test_executor_hinted_retry import _ScriptProvider
from tests.test_executor_retry_continue import _SINGLE_WF, _WRITER, _make_quest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_backoff_loop_arms_wait_and_consumes_retry_now(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    # One blind (hint-less) rate_limit failure, then success on the re-drive.
    provider = _ScriptProvider([(True, None)])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    seen: dict[str, Any] = {}

    async def _fake_sleep(_delay: float, coord: Any, **_kw: Any) -> None:
        # The loop must have armed the wait before sleeping (mirrors the live
        # reader firing during the countdown), and the arm survives into here.
        seen["armed_during_sleep"] = coord.backoff_wait_active
        seen["coord"] = coord
        coord.request_retry_now()
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _fake_sleep)

    result = await run_quest(quest)

    assert result is RunResult.COMPLETED        # re-drove to success, did NOT pause
    assert provider.drives == 2                  # failure then success
    assert seen["armed_during_sleep"] is True    # begin_backoff_wait ran before the sleep
    assert seen["coord"].backoff_wait_active is False   # end_backoff_wait ran after
    assert seen["coord"].retry_now_requested is False   # loop consumed the latch


@pytest.mark.anyio
async def test_backoff_wait_disarmed_even_when_sleep_raises(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _ScriptProvider([(True, None)])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    seen: dict[str, Any] = {}

    async def _fake_sleep(_delay: float, coord: Any, **_kw: Any) -> None:
        seen["coord"] = coord
        coord.request_retry_now()
        raise RuntimeError("sleep interrupted by test")

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _fake_sleep)

    result = await run_quest(quest)

    # run_quest's last-resort except-Exception handler catches the propagated raise
    # and returns PAUSED rather than propagating further.
    assert result is RunResult.PAUSED
    # The KEY guarantee: end_backoff_wait() ran in the finally despite the exception.
    assert seen["coord"].backoff_wait_active is False


@pytest.mark.anyio
async def test_backoff_ended_emitted_with_retry_now_reason(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _ScriptProvider([(True, None)])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _fake_sleep(_delay: float, coord: Any, **_kw: Any) -> None:
        coord.request_retry_now()
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _fake_sleep)

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    ended = [e for e in events if e["type"] == "backoff_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"] == {"reason": "retry_now"}
    assert ended[0]["agent_id"] is None
    # The end marker follows its paired agent_retry, one per sleep.
    assert [e["type"] for e in events
            if e["type"] in ("agent_retry", "backoff_ended")] == [
        "agent_retry", "backoff_ended"]


@pytest.mark.anyio
async def test_backoff_ended_emitted_with_elapsed_reason_on_natural_expiry(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _ScriptProvider([(True, None)])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _fake_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        return None
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _fake_sleep)

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    ended = [e for e in events if e["type"] == "backoff_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"] == {"reason": "elapsed"}
    assert ended[0]["agent_id"] is None
