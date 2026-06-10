from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(root: Path, *, slug: str = "c", title: str = "t") -> Change:
    import contextlib
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config
    src_wf = root / "imp" / "workflows" / "default"
    change_dir = root / ".imp" / "change"
    (change_dir / "state").mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": slug, "title": title, "base_branch": "main"}))
    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()
    workflow = load_workflow(src_wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(id=slug, step=flat[0].name if flat else None, step_attempt=0,
          status="running", vars=dict(workflow.vars.defaults())).save(change_dir / "state")
    change = Change(id=slug, title=title, base_branch="main",
                    control_root=root, project_root=root, change_dir=change_dir)
    change.event_log().append(agent_id=None, type="change_created",
                              payload={"id": slug, "slug": change.slug, "title": title})
    return change


def _seed_single_agent(root: Path) -> Change:
    wf = root / "imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: medium}\n")
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    return _make_change(root)


@pytest.mark.anyio
async def test_mid_turn_interrupt_sets_resume_pending(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # Simulate ESC mid-turn: flip should_pause, then yield ONE event so
            # drive_invocation's per-event should_pause check observes it and
            # returns paused (the clean cooperative interrupt).
            await coord.signal_pause(reason="esc")
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _InterruptingRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_change(change, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.status == "paused"
    assert state.resume_pending == state.step  # transcript kept on re-entry


@pytest.mark.anyio
async def test_genuine_fatal_no_interrupt_keeps_fresh_restart(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _AuthRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "auth", "kind": "AuthError", "message": "401"}}
        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _AuthRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())
    result = await run_change(change)  # no coordinator interrupt
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.resume_pending is None  # fatal, not an interrupt → fresh restart
