from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.executor import RunResult, run_quest
from imp.engine.state import State
from tests.test_executor_pause_reason import _make_change, _seed_single_agent


def _seed_merge_step(root: Path):
    """A quest whose sole step is the engine-native merge gate, so a REFUSED
    merge drives a reason='merge_blocked' Pause through run_quest's generic
    pause site (the halt_condition gate under test)."""
    wf = root / "imp" / "workflows" / "imp"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: merge-to-base\n"
        "        kind: merge_and_exit_worktree\n"
        "        label: merging\n"
        "        max_loops: 3\n"
        "        rules:\n"
        "          - {if: {merge: MERGED}, continue: true}\n"
        "          - {if: {merge: REFUSED}, pause: true}\n"
        "          - {else: true, pause: true}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    return _make_change(root)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _register(runner_cls: type) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return runner_cls()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _P())


@pytest.mark.anyio
async def test_fatal_error_pause_writes_error_halt(tmp_path: Path) -> None:
    quest = _seed_single_agent(tmp_path)

    class _FatalRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "terminated", "kind": "TerminatedError",
                "message": "boom"}}

        async def interrupt(self) -> None:
            pass

    _register(_FatalRunner)
    result = await run_quest(quest, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    assert State.load(quest.state_dir).status == "error"
    assert State.load(quest.state_dir).halt_condition is None


@pytest.mark.anyio
async def test_awaiting_user_park_writes_clean_halt(tmp_path: Path) -> None:
    quest = _seed_single_agent(tmp_path)
    from imp.engine.awaiting_user_input import AwaitingUserInput

    class _AwaitingRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            raise AwaitingUserInput("last question", interrupt=False)
            yield  # unreachable — makes run() an async generator

        async def interrupt(self) -> None:
            pass

    _register(_AwaitingRunner)
    result = await run_quest(quest, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    assert State.load(quest.state_dir).halt_condition is None


@pytest.mark.anyio
async def test_ctrl_d_interrupt_writes_clean_halt(tmp_path: Path) -> None:
    quest = _seed_single_agent(tmp_path)
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            await coord.signal_pause(reason="ctrl-d")
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    _register(_InterruptingRunner)
    result = await run_quest(quest, on_event=lambda _e: None, coordinator=coord)
    assert result is RunResult.PAUSED
    assert State.load(quest.state_dir).halt_condition is None


@pytest.mark.anyio
async def test_merge_blocked_pause_writes_no_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REFUSED merge pauses with status='merge_blocked'; because that status is
    outside the clean pause set, the raw classifier would return 'error' — the
    gate must instead write halt_condition=None."""
    quest = _seed_merge_step(tmp_path)

    class _UnusedRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_result", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    _register(_UnusedRunner)
    from imp.engine import worktree
    monkeypatch.setattr(
        worktree, "merge_to_base",
        lambda *a, **k: worktree.MergeResult("REFUSED", detail="fatal: refusing"))

    result = await run_quest(quest, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    st = State.load(quest.state_dir)
    assert st.status == "merge_blocked"
    assert st.halt_condition is None


@pytest.mark.anyio
async def test_resume_entry_clears_stale_halt_before_work(tmp_path: Path) -> None:
    quest = _seed_single_agent(tmp_path)
    # Simulate a home killed while parked at a gate: a stale "waiting" on disk.
    st = State.load(quest.state_dir)
    st.halt_condition = "waiting"
    st.save(quest.state_dir)

    seen: list[object] = []

    class _PeekRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # By the time the step dispatches, work has resumed: the stale halt
            # must already be cleared on disk (not read as a live "waiting").
            seen.append(State.load(quest.state_dir).halt_condition)
            raise __import__("imp.engine.awaiting_user_input",
                             fromlist=["AwaitingUserInput"]).AwaitingUserInput(
                "q", interrupt=False)
            yield  # unreachable

        async def interrupt(self) -> None:
            pass

    _register(_PeekRunner)
    await run_quest(quest, on_event=lambda _e: None)
    assert seen == [None]
