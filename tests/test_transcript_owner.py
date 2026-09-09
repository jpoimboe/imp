from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.executor import RunResult, run_quest
from imp.engine.reincarnation import read_transcript_owner, write_transcript_owner
from imp.engine.state import State
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
            seen.append(read_transcript_owner(tdir))
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
async def test_a_fresh_dispatch_labels_the_transcript_with_its_resolved_vendor(
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


@pytest.mark.anyio
async def test_a_replayed_transcript_keeps_the_owner_that_wrote_it(
        tmp_path: Path, provider_registry) -> None:
    # The kept branch writes no label: the sidecar describes the transcript on
    # disk, so a replay must not relabel a foreign one as its own. A relabelling
    # implementation records "fake" here.
    quest = _seed_single_agent(tmp_path)
    state = State.load(quest.state_dir)
    state.resume_pending = state.step
    state.save(quest.state_dir)
    tdir = quest.quest_dir / "steps" / "only" / "w"
    tdir.mkdir(parents=True, exist_ok=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="assistant", content=(TextBlock(text="still working"),)))
    write_transcript_owner(tdir, "claude_code")
    quest.event_log().append(
        agent_id="step:only/w", type="engine_message",
        payload={"role": "system", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "test"}]})
    seen: list[str | None] = []
    provider_registry("fake", env_check=lambda: True,
                      factory=_recording_factory(tdir, seen))

    result = await run_quest(quest)

    assert result is RunResult.COMPLETED
    assert seen == ["claude_code"]
