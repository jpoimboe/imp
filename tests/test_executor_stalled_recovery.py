"""A watchdog-class 'stalled' error retries with budgeted backoff through the
executor's self-healed-retry path: it emits agent_retry per stall without burning
step_attempt and recovers without pausing, and once cumulative backoff exceeds the
1800s budget it pauses cleanly as retry_ceiling (not the auth-only reauth_needed)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.quest import Quest
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(root: Path) -> Quest:
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )

    quest_dir = root / ".imp" / "change"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "state").mkdir(exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id="c", step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(quest_dir / "state")

    change = Quest(
        id="c", title="t", base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created", payload={"id": "c", "title": "t"})
    return change


class _StallNthRunner:
    """Stalls (emits a 'stalled' error) when `stalled`, else succeeds."""

    def __init__(self, stalled: bool) -> None:
        self._stalled = stalled

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        if self._stalled:
            yield {"type": "error", "payload": {
                "category": "stalled", "kind": "stalled",
                "message": "no events for 240s"}}
        else:
            yield {"type": "agent_output", "payload": {"status": "Success", "outcome": "done"}}

    async def interrupt(self) -> None:
        pass


class _StallThenSucceedProvider:
    name = "fake"
    model = "m"

    def __init__(self, stalls: int) -> None:
        self.stalls = stalls
        self.drives = 0

    def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
        self.drives += 1
        return _StallNthRunner(self.drives <= self.stalls)


@pytest.mark.anyio
async def test_stalled_retries_with_backoff_without_burning_step_attempt(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path)

    provider = _StallThenSucceedProvider(stalls=3)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    # max_retries=1: completing after THREE stalls is only possible on the
    # backoff path. The errored fallthrough (step_attempt += 1) would pause after
    # the 2nd attempt with zero agent_retry events.
    assert result is RunResult.COMPLETED
    assert provider.drives == 4   # 3 stalls + 1 success

    retries = [e for e in events if e["type"] == "agent_retry"]
    assert len(retries) == 3
    assert [r["payload"]["category"] for r in retries] == ["stalled"] * 3
    assert [r["payload"]["attempt"] for r in retries] == [1, 2, 3]
    assert [r["payload"]["backoff_seconds"] for r in retries] == [1.0, 2.0, 4.0]

    assert not any(e["type"] == "workflow_paused" for e in events)
    assert not any(
        e["type"] == "error" and e["payload"].get("category") == "config"
        for e in events)

    state = State.load(change.state_dir)
    assert state.status == "completed"
    assert state.step_attempt == 0


class _AlwaysStallProvider:
    name = "fake"
    model = "m"

    def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
        return _StallNthRunner(stalled=True)


@pytest.mark.anyio
async def test_stalled_backoff_pauses_as_retry_ceiling_on_budget_exhaustion(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _AlwaysStallProvider())

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    # The loop terminates (no infinite retry) once cumulative backoff exceeds the
    # 1800s stalled budget, pausing as a generic ceiling — not the auth-only
    # reauth_needed.
    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["reason"] == "retry_ceiling"
    assert paused[0]["payload"]["reason"] != "reauth_needed"

    state = State.load(change.state_dir)
    assert state.status == "paused"
    assert state.step_attempt == 0   # the backoff path never burned it
