"""Reset-aware retry: _route_agent exempts a hinted (reset-time) delay from the
per-category budget, capped at 3 such sleeps per loop, and tags agent_retry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.engine.executor import RunResult, run_change
from tests.test_executor_retry_continue import _SINGLE_WF, _WRITER, _make_change


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _ScriptRunner:
    """One drive: a rate_limit error (with an optional retry_after hint) when
    `fail`, else a success."""

    def __init__(self, fail: bool, hint: float | None) -> None:
        self._fail = fail
        self._hint = hint

    async def run(self, invocation: Any) -> Any:
        if self._fail:
            detail = ({"retry_after_seconds": self._hint}
                      if self._hint is not None else {})
            yield {"type": "error", "payload": {
                "category": "rate_limit", "kind": "429",
                "message": "slow down", "detail": detail}}
        else:
            yield {"type": "agent_output",
                   "payload": {"status": "Success", "outcome": "done"}}

    async def interrupt(self) -> None:
        pass


class _ScriptProvider:
    """Returns a runner per a per-drive script of (fail, hint) tuples; once the
    script is exhausted every further drive succeeds."""

    name = "fake"
    model = "m"

    def __init__(self, script: list[tuple[bool, float | None]]) -> None:
        self._script = script
        self.drives = 0

    def create_runner(self, _inv: Any) -> _ScriptRunner:
        i = self.drives
        self.drives += 1
        if i < len(self._script):
            fail, hint = self._script[i]
            return _ScriptRunner(fail, hint)
        return _ScriptRunner(fail=False, hint=None)


@pytest.mark.anyio
async def test_hinted_delay_is_budget_exempt_and_completes(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    # Three hinted (7200s > 1800s rate_limit budget) failures, then success.
    # Without the exemption the first 7200s delay blows the budget and pauses on
    # drive 1; the exemption lets all three ride and the 4th drive succeed.
    provider = _ScriptProvider([(True, 7200.0), (True, 7200.0), (True, 7200.0)])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _no_sleep(_delay: float, _coord: Any) -> None:
        return None
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED
    assert provider.drives == 4
    retries = [e for e in events if e["type"] == "agent_retry"]
    assert len(retries) == 3
    assert all(e["payload"]["hinted"] is True for e in retries)
    assert all(e["payload"]["backoff_seconds"] == 7200.0 for e in retries)


@pytest.mark.anyio
async def test_fourth_hinted_sleep_falls_back_to_budgeted(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    # Six hinted 7200s failures available. The first three are exempt; the fourth
    # is budgeted (cap=3) and 7200s > the 1800s budget, so the loop pauses at the
    # retry ceiling BEFORE emitting a fourth agent_retry.
    provider = _ScriptProvider([(True, 7200.0)] * 6)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _no_sleep(_delay: float, _coord: Any) -> None:
        return None
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert paused and paused[0]["payload"]["reason"] == "retry_ceiling"
    assert provider.drives == 4
    retries = [e for e in events if e["type"] == "agent_retry"]
    assert len(retries) == 3
    assert all(e["payload"]["hinted"] is True for e in retries)


@pytest.mark.anyio
async def test_blind_retries_after_hinted_sleep_still_reach_ceiling(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    # One hinted 7200s failure (exempt, NOT added to cumulative_backoff), then
    # blind (hint-less) rate_limit failures. With a tiny 5s budget the blind
    # schedule (2s then 4s) reaches the ceiling on the second blind retry. Had the
    # hinted 7200s accrued, the first blind check would pause immediately — so a
    # second retry proves the hinted wait was never budgeted.
    provider = _ScriptProvider([(True, 7200.0)] + [(True, None)] * 6)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    monkeypatch.setattr("imp.engine.executor.budget_for", lambda _category: 5.0)

    async def _no_sleep(_delay: float, _coord: Any) -> None:
        return None
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.PAUSED
    retries = [e for e in events if e["type"] == "agent_retry"]
    # retry 1: hinted (exempt); retry 2: blind 2s (cumulative 0 -> 2, within 5s).
    assert len(retries) == 2
    assert retries[0]["payload"]["hinted"] is True
    assert "hinted" not in retries[1]["payload"]
    assert retries[1]["payload"]["backoff_seconds"] == 2.0
