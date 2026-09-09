from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine import executor as executor_mod
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.reincarnation import handoff_path_for
from imp.engine.state import State
from imp.providers import BUILTIN_FLOOR
from tests.test_executor import _persist_initial, _seed_minimal_workflow
from tests.test_executor_provider_pin import _floor_config, _reopen
from tests.test_provider_reconcile_points import _TWO_STEPS, _pre_resolved


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_PICKS_UP = "provider switched — codex picks up this step from the event log"
_NEXT_STEP = "provider switched — codex takes the next step"


def _factory(seen: list[str], quest: Quest, *, arm_on: int,
             script: Callable[[int, AgentInvocation], list[Event]]):
    """A provider whose runner records the model it resolved to, arms the pin
    from inside invocation `arm_on` (what the daemon's set_provider callback
    does to the live Quest), and then yields whatever `script` returns."""

    class _Runner:
        # Public attribute only — see tests/test_no_private_reachthroughs.py.
        def __init__(self, model: str) -> None:
            self.model = model

        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            n = len(seen) + 1
            seen.append(self.model)
            if n == arm_on:
                quest.set_provider("codex")
            for event in script(n, inv):
                yield event

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "x"

        def __init__(self, model: str) -> None:
            self.model = model

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner(self.model)

    return lambda model: _P(model)


@pytest.mark.anyio
async def test_a_completed_step_confirms_the_switch_takes_the_next_step(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(_TWO_STEPS)
    seen: list[str] = []
    stashed: list[list[str]] = []

    def _script(_n: int, _inv: AgentInvocation) -> list[Event]:
        stashed.append(sorted(
            p.name for p in (quest.quest_dir / "steps" / "only").glob("*.attempt-*")))
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]
    assert {"type": "script_output", "payload": {"stdout": _NEXT_STEP}} in events
    assert not [e for e in events if e["type"] == "reincarnating"]
    assert stashed == [[], []]
    assert {pm.provider for pm in _reopen(quest).resolved_tiers.values()} == {"codex"}


def _error(category: str, kind: str) -> list[Event]:
    return [{"type": "error", "payload": {
        "category": category, "kind": kind, "message": "x"}}]


def _handoff(_n: int, inv: AgentInvocation) -> list[Event]:
    _persist_initial(inv)
    hp = handoff_path_for(inv.transcript_dir)
    hp.write_text("# handoff\n")
    return [{"type": "handoff", "payload": {"handoff_path": str(hp)}}]


_DISPOSITIONS = {
    "fatal": (1, lambda _n, _i: _error("api", "api_error"), None),
    "result_paused": (1, lambda _n, _i: _error("api", "interrupted"), None),
    "on_max_retries": (2, lambda _n, _i: _error("agent_output_missing", "missing"),
                       None),
    "retry_ceiling": (1, lambda _n, _i: _error("rate_limit", "RateLimitError"),
                      "budget"),
    "reincarnation_cap": (1, _handoff, "cap"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(_DISPOSITIONS))
async def test_every_returning_disposition_reconciles_before_it_returns(
    tmp_path: Path, floor_availability, monkeypatch, name: str,
) -> None:
    arm_on, script, extra = _DISPOSITIONS[name]
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    if extra == "budget":
        monkeypatch.setattr(executor_mod, "budget_for", lambda _c: 0.5)
    seen: list[str] = []
    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=arm_on, script=script))
    _pre_resolved(quest, "claude_code")
    if extra == "cap":
        # run_quest only reads context.max_reincarnations on a FRESH resolve
        # (executor.py:537), and these quests are already resolved.
        state = State.load(quest.state_dir)
        state.max_reincarnations = 0
        state.save(quest.state_dir)
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.PAUSED

    assert {pm.provider
            for pm in State.load(quest.state_dir).resolved_tiers.values()} == {"codex"}
    assert {"type": "script_output",
            "payload": {"stdout": _PICKS_UP}} in events


@pytest.mark.anyio
async def test_a_pause_after_the_backoff_sleep_applies_the_switch_on_its_way_out(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    async def _pausing(_delay: float, coord: Any, **_kw: Any) -> None:
        await coord.signal_pause()

    monkeypatch.setattr(executor_mod, "_interruptible_sleep", _pausing)
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    floor_availability(
        {"claude_code", "codex"},
        factory=_factory(seen, quest, arm_on=1,
                         script=lambda _n, _i: _error("rate_limit", "RateLimitError")))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.PAUSED

    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"]]
    assert {pm.provider
            for pm in State.load(quest.state_dir).resolved_tiers.values()} == {"codex"}
    assert {"type": "script_output", "payload": {"stdout": _PICKS_UP}} in events
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["elapsed"]
