from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.abandon import archive_conversation
from imp.engine.archive.read import rehydrate_change
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_paused_conversation_round_trips_and_replays_on_resume(
        tmp_path: Path, provider_registry) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Conv", change_id="conv", base_branch="main")

    # _seed_control workflow: phase "p", step "s"
    tdir = ch.change_dir / "steps" / "p" / "s"
    tdir.mkdir(parents=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="assistant", content=(TextBlock(text="still working"),)))
    # step_context lets _resume_system_prompt find the stored prompt on resume
    ch.event_log().append(
        agent_id="step:p/s", type="step_context",
        payload={"system_prompt": "seeded prompt"})
    seeded_bytes = (tdir / "transcript.jsonl").read_bytes()

    archive_conversation(ch)
    assert not (tmp_path / ".imp" / "changes" / "conv").exists()

    change_dir = rehydrate_change(tmp_path, "conv")
    restored = change_dir / "steps" / "p" / "s" / "transcript.jsonl"
    assert restored.read_bytes() == seeded_bytes

    change = Change.load(tmp_path, "conv")
    state = State.load(change.state_dir)
    state.resume_pending = state.step   # "s" → replay, not re-seed
    state.save(change.state_dir)

    seen_initial_empty: list[bool] = []
    seen_transcript_nonempty: list[bool] = []

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            seen_initial_empty.append(len(inv.initial_messages) == 0)
            seen_transcript_nonempty.append(
                Transcript(restored).exists_and_nonempty())
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    # _seed_control configures providers: [anthropic] and tiers: {anthropic: x}
    class _P:
        name = "anthropic"
        model = "x"

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()  # type: ignore[return-value]

    provider_registry("anthropic", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_change(change)

    assert result is RunResult.COMPLETED
    assert seen_initial_empty == [True]
    assert seen_transcript_nonempty == [True]

    lines = change.events_path.read_text().splitlines()
    events = [json.loads(ln) for ln in lines if ln.strip()]
    starts = [e for e in events if e["type"] == "step_started"]
    assert any(
        e["payload"].get("step") == "s" and e["payload"].get("resumed") is True
        for e in starts
    )
