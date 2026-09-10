from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.executor import RunResult, run_quest
from imp.engine.reincarnation import read_session_owner
from tests.test_executor_resume_rerun import _seed_single_agent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _recording_factory(
    tdir: Path, seen: list[str | None],
) -> Callable[[str], object]:
    """Captures the sidecar inside the session: a completing run rmtree's the quest home."""

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            seen.append(read_session_owner(tdir))
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    return lambda _m: _P()


@pytest.mark.anyio
async def test_a_fresh_dispatch_labels_the_session_with_its_resolved_vendor(
        tmp_path: Path, provider_registry) -> None:
    # "fake" is what the seeded config resolves every tier to, so the assertion
    # pins the resolved vendor rather than any literal the writer could echo.
    quest = _seed_single_agent(tmp_path)
    tdir = quest.quest_dir / "steps" / "only" / "w"
    seen: list[str | None] = []
    provider_registry("fake", env_check=lambda: True,
                      factory=_recording_factory(tdir, seen))

    result = await run_quest(quest)

    assert result is RunResult.COMPLETED
    assert seen == ["fake"]        # labelled before the session it describes ran
