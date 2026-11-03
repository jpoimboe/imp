from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.cli.signals import InterruptCoordinator
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_AGENT_MD = ("---\nname: a\ntools: [complete_step]\n"
             "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
_SUCCESS: Event = {"type": "agent_result", "payload": {"outcome": "ok", "status": "Success"}}


def _make_quest(root: Path, steps: list[str]) -> Quest:
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "a.md").write_text(_AGENT_MD)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n  - name: only\n    steps:\n"
        + "".join(f"      - {{name: {s}, file: agents/a.md, tier: low}}\n" for s in steps))
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")

    quest_id = "c"
    quest_dir = project_state_dir(root) / "quests" / quest_id
    (quest_dir / "state").mkdir(parents=True)
    (quest_dir / "quest.json").write_text(json.dumps(
        {"id": quest_id, "title": "t", "base_branch": "main"}))
    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(control_root=root).config.tier_names()
    workflow = load_workflow(wf, configured_tiers=configured_tiers)
    State(id=quest_id, step=steps[0], step_attempt=0, status="running",
          vars=dict(workflow.vars.defaults()), loop_counts={}).save(quest_dir / "state")
    quest = Quest(id=quest_id, title="t", base_branch="main",
                  control_root=root, project_root=root, quest_dir=quest_dir)
    quest.event_log().append(agent_id=None, type="quest_created",
                             payload={"id": quest_id, "title": "t"})
    return quest


def _register(run: Callable[[AgentInvocation], AsyncIterator[Event]]) -> None:
    from imp.providers import resolve

    class _Runner:
        def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            return run(inv)

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.reset_providers_for_tests()
    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())


def _paused(quest: Quest) -> list[dict[str, Any]]:
    return [json.loads(line)["payload"] for line in quest.events_path.read_text().splitlines()
            if line.strip() and json.loads(line)["type"] == "workflow_paused"]


def _interrupt_on_completion_of(
    coord: InterruptCoordinator, step: str, leave: str | None,
) -> Callable[[dict[str, Any]], None]:
    def _on_event(e: dict[str, Any]) -> None:
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == step):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"
            coord.leave_reason = leave
    return _on_event


_LEAVES = pytest.mark.parametrize(
    "leave,expected", [("ctrl-d", "ctrl-d"), (None, "ctrl-c")])


@_LEAVES
@pytest.mark.anyio
async def test_mid_flight_pause_records_leave_reason(
    tmp_path: Path, leave: str | None, expected: str,
) -> None:
    quest = _make_quest(tmp_path, ["w"])
    coord = InterruptCoordinator()

    async def _run(_inv: AgentInvocation) -> AsyncIterator[Event]:
        await coord.signal_pause(reason="ctrl-c")
        coord.leave_reason = leave
        raise AwaitingUserInput("q", interrupt=True)
        yield _SUCCESS  # unreachable: makes this an async generator

    _register(_run)
    assert await run_quest(quest, coordinator=coord) is RunResult.PAUSED
    assert _paused(quest) == [{"reason": "interrupt", "interrupt_reason": expected}]
    assert State.load(quest.state_dir).status == "paused"


@_LEAVES
@pytest.mark.anyio
async def test_post_navigation_park_records_leave_reason(
    tmp_path: Path, leave: str | None, expected: str,
) -> None:
    quest = _make_quest(tmp_path, ["s1", "s2"])
    coord = InterruptCoordinator()

    async def _run(_inv: AgentInvocation) -> AsyncIterator[Event]:
        yield _SUCCESS

    _register(_run)
    result = await run_quest(
        quest, on_event=_interrupt_on_completion_of(coord, "s1", leave),
        coordinator=coord)
    assert result is RunResult.PAUSED
    assert _paused(quest) == [{"reason": "interrupt", "interrupt_reason": expected}]
    assert State.load(quest.state_dir).step == "s2"


@_LEAVES
@pytest.mark.anyio
async def test_completion_park_records_leave_reason(
    tmp_path: Path, leave: str | None, expected: str,
) -> None:
    quest = _make_quest(tmp_path, ["w"])
    coord = InterruptCoordinator()

    async def _run(_inv: AgentInvocation) -> AsyncIterator[Event]:
        yield _SUCCESS

    _register(_run)
    result = await run_quest(
        quest, on_event=_interrupt_on_completion_of(coord, "w", leave),
        coordinator=coord)
    assert result is RunResult.PAUSED
    assert _paused(quest) == [{"reason": "interrupt", "interrupt_reason": expected}]
    assert State.load(quest.state_dir).completion_pending is True


@pytest.mark.anyio
async def test_leave_reason_without_interrupt_does_not_relabel(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, ["w"])
    coord = InterruptCoordinator()
    coord.leave_reason = "ctrl-d"

    async def _run(_inv: AgentInvocation) -> AsyncIterator[Event]:
        raise AwaitingUserInput("q", interrupt=False)
        yield _SUCCESS  # unreachable: makes this an async generator

    _register(_run)
    assert await run_quest(quest, coordinator=coord) is RunResult.PAUSED
    paused = _paused(quest)
    assert len(paused) == 1
    assert paused[0]["reason"] == "awaiting_user"
    assert "interrupt_reason" not in paused[0]
