"""The engine-side dependency wait: the provider call is torn down, the engine
polls backlog.readiness unbounded, then re-enters the step on a fresh call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, TextBlock
from imp.agents.transcript import Transcript
from imp.cli.signals import InterruptCoordinator
from imp.engine.executor import RunResult, run_quest
from imp.engine.reincarnation import write_session_owner
from imp.engine.state import State
from tests.test_executor_retry_continue import _SINGLE_WF, _WRITER, _make_quest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _DepWaitRunner:
    """Yields the dependency_wait control event while `wait`, else succeeds.
    Records its own teardown so tests can order it against the engine's poll."""

    def __init__(self, wait: bool, trace: list[str]) -> None:
        self._wait = wait
        self._trace = trace

    async def run(self, invocation: Any) -> Any:
        try:
            if self._wait:
                yield {"type": "dependency_wait",
                       "payload": {"depends_on": ["jibu68"]}}
            else:
                yield {"type": "agent_result",
                       "payload": {"status": "Success", "outcome": "done"}}
        finally:
            self._trace.append("closed")

    async def interrupt(self) -> None:
        pass


class _DepProvider:
    """Register ONE hoisted instance per run: provider_from_model rebuilds the
    provider on every invocation (resolve.py:96-105, no caching), so a
    fresh-per-call factory would never advance `drives` and the step would wait
    forever."""

    name = "fake"
    model = "m"

    def __init__(self, waits: int, trace: list[str] | None = None) -> None:
        self.waits = waits
        self.drives = 0
        self.trace = trace if trace is not None else []
        self.causes: list[str] = []
        self.feedback: list[str | None] = []

    def create_runner(self, inv: Any) -> _DepWaitRunner:
        self.drives += 1
        self.causes.append(inv.resume_cause)
        self.feedback.append(inv.retry_feedback)
        return _DepWaitRunner(self.drives <= self.waits, self.trace)


class _ScriptedRunner:
    """One scripted outer event per drive: "wait" | "stall" | "ok"."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    async def run(self, invocation: Any) -> Any:
        if self._kind == "wait":
            yield {"type": "dependency_wait",
                   "payload": {"depends_on": ["jibu68"]}}
        elif self._kind == "stall":
            yield {"type": "error", "payload": {
                "category": "stalled", "kind": "stalled",
                "message": "no events for 240s"}}
        else:
            yield {"type": "agent_result",
                   "payload": {"status": "Success", "outcome": "done"}}

    async def interrupt(self) -> None:
        pass


class _ScriptedProvider:
    name = "fake"
    model = "m"

    def __init__(self, script: list[str]) -> None:
        self._script = script
        self.drives = 0

    def create_runner(self, _inv: Any) -> _ScriptedRunner:
        kind = self._script[self.drives]
        self.drives += 1
        return _ScriptedRunner(kind)


def _quest(tmp_path: Path) -> Any:
    return _make_quest(tmp_path, agents={"writer.md": _WRITER},
                       workflow_yaml=_SINGLE_WF)


def _merges_after(monkeypatch: pytest.MonkeyPatch, n: int,
                  trace: list[str] | None = None) -> dict[str, int]:
    """backlog.readiness reports jibu68 unmerged for the first n-1 calls."""
    polls = {"n": 0}

    def _readiness(_cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        polls["n"] += 1
        if trace is not None:
            trace.append("poll")
        return (True, []) if polls["n"] >= n else (False, ["jibu68"])

    monkeypatch.setattr("imp.engine.backlog.readiness", _readiness)
    return polls


@pytest.mark.anyio
async def test_call_is_closed_before_the_wait_and_step_reenters(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    trace: list[str] = []
    provider = _DepProvider(waits=1, trace=trace)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    _merges_after(monkeypatch, 2, trace)

    async def _no_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    assert provider.drives == 2                 # a FRESH call after the merge
    assert trace[:2] == ["closed", "poll"]      # torn down BEFORE the first poll
    waiting = [e for e in events if e["type"] == "dependency_waiting"]
    ready = [e for e in events if e["type"] == "dependency_ready"]
    assert len(waiting) == 1 and len(ready) == 1
    assert waiting[0]["payload"] == {"depends_on": ["jibu68"]}
    assert waiting[0]["agent_id"] == "step:only/w"
    assert ready[0]["payload"] == {}
    assert ready[0]["agent_id"] == "step:only/w"
    # The re-entry is armed as a dependency resume, not a human pause.
    assert provider.causes == ["human", "dependency"]


@pytest.mark.anyio
async def test_resume_intent_is_on_disk_before_the_first_sleep(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    provider = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    _merges_after(monkeypatch, 3)
    seen: list[tuple[str | None, bool]] = []

    async def _capture_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        st = State.load(quest.state_dir)
        seen.append((st.resume_pending, st.resume_should_continue))

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep",
                        _capture_sleep)

    assert await run_quest(quest) is RunResult.COMPLETED
    assert seen[0] == ("w", True)


@pytest.mark.anyio
async def test_wait_is_unbounded_and_is_not_a_provider_retry(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    provider = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    polls = _merges_after(monkeypatch, 40)      # far past any retry budget
    sleeps = {"n": 0}

    async def _count_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        sleeps["n"] += 1

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _count_sleep)

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    assert polls["n"] == 40 and sleeps["n"] == 39
    assert provider.drives == 2
    # Not a retry: no backoff counter, no agent_retry/backoff_ended bracket.
    assert [e for e in events
            if e["type"] in ("agent_retry", "backoff_ended")] == []


@pytest.mark.anyio
async def test_wait_does_not_consume_the_provider_retry_budget(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    provider = _ScriptedProvider(["wait", "stall", "ok"])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    _merges_after(monkeypatch, 1)          # merged on the first poll

    async def _no_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert provider.drives == 3
    assert [e["type"] for e in events if e["type"].startswith("dependency")] \
        == ["dependency_waiting", "dependency_ready"]
    retries = [e["payload"] for e in events if e["type"] == "agent_retry"]
    # The post-wait error is the run's FIRST backoff attempt. The ladder is
    # min(60, 2 ** prior_backoff_attempts) (error_policy.decide), so an
    # untouched counter yields attempt 1 at the 1.0s rung; had the wait
    # incremented `backoff`, this would read attempt 2 at 2.0s.
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["backoff_seconds"] == 1.0


@pytest.mark.anyio
async def test_headless_run_waits_through_on_its_own(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    provider = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    polls = _merges_after(monkeypatch, 3)

    async def _no_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    result = await run_quest(quest, headless=True)

    assert result is RunResult.COMPLETED
    assert provider.drives == 2
    # Nobody is attached: the run itself polled the wait through to the merge.
    assert polls["n"] == 3


@pytest.mark.anyio
async def test_ctrl_d_pauses_the_wait_and_resume_retries_it(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    provider = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    monkeypatch.setattr("imp.engine.backlog.readiness",
                        lambda _cr, deps: (False, ["jibu68"]))
    coord = InterruptCoordinator()

    async def _pause_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        await coord.signal_pause(reason="ctrl-d")

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _pause_sleep)

    events: list[dict] = []
    result = await run_quest(quest, coordinator=coord, on_event=events.append)

    assert result is RunResult.PAUSED
    assert [e for e in events if e["type"] == "dependency_ready"] == []
    st = State.load(quest.state_dir)
    assert st.resume_pending == "w" and st.resume_should_continue is True

    # A later resume retries the wait and, once merged, proceeds.
    provider2 = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider2)
    _merges_after(monkeypatch, 2)

    async def _no_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    assert await run_quest(quest, coordinator=InterruptCoordinator()) \
        is RunResult.COMPLETED
    assert provider2.drives == 2


@pytest.mark.anyio
async def test_retry_now_gesture_cannot_collapse_the_poll(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    provider = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    polls = _merges_after(monkeypatch, 5)
    coord = InterruptCoordinator()
    delays: list[float] = []
    actives: list[bool] = []

    async def _sleep(delay: float, _coord: Any, **_kw: Any) -> None:
        delays.append(delay)
        actives.append(coord.backoff_wait_active)
        coord.request_retry_now()          # a stray `r` mid-wait

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _sleep)

    assert await run_quest(quest, coordinator=coord) is RunResult.COMPLETED
    # AC 7(a): the readiness-call count over a fixed number of sleeps is
    # unchanged by the request — one poll per sleep plus the clearing poll,
    # each at the full interval, never a spin.
    assert polls["n"] == len(delays) + 1 == 5
    assert delays == [2.0, 2.0, 2.0, 2.0]
    assert actives == [False, False, False, False]
    assert coord.retry_now_requested is False   # no latch leaks into a real backoff


@pytest.mark.anyio
async def test_reentry_does_not_redeliver_retry_feedback(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _quest(tmp_path)
    # Enter the step as a resume with a kept transcript, so drive 1 carries the
    # rework wrapper as retry_feedback.
    tdir = quest.quest_dir / "steps" / "only" / "w"
    tdir.mkdir(parents=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="user", content=(TextBlock(text="go"),)))
    write_session_owner(tdir, "fake")
    # Seed the engine_message/system_prompt event so _resume_system_prompt
    # finds the stored prompt (in production this is always present when a
    # transcript exists).
    quest.event_log().append(
        agent_id="step:only/w", type="engine_message",
        payload={"role": "system", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "test"}]})
    st = State.load(quest.state_dir)
    st.resume_pending = "w"
    st.resume_should_continue = True
    st.retry_feedback = {"target_step": "w", "text": "REVISE: tighten the plan"}
    st.save(quest.state_dir)

    provider = _DepProvider(waits=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    _merges_after(monkeypatch, 2)

    async def _no_sleep(_delay: float, _coord: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    assert await run_quest(quest) is RunResult.COMPLETED
    assert provider.drives == 2
    assert provider.feedback[0] is not None
    assert "REVISE: tighten the plan" in provider.feedback[0]
    assert provider.feedback[1] is None     # not re-delivered after the wait
