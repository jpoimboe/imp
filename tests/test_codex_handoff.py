from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.completion import build_continuation_fn
from imp.engine.reincarnation import handoff_path_for
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_rollout import RolloutReadings
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, agent_message, thread_started, turn_completed

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)


def _seed_git_and_auth(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC, tier=None)


@pytest.mark.anyio
async def test_threshold_crossing_elicits_codex_handoff(
        tmp_path: Path, fake_codex_rollout_readings: list[RolloutReadings]) -> None:
    """A codex turn at the 160000/400000 = 0.40 cutoff elicits the
    handoff instruction; the following turn yields the terminal event."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    hp = handoff_path_for(tmp_path / "tx")
    fake_codex_rollout_readings[0] = RolloutReadings(
        context=(160_000, 400_000), quota=None)

    scripted = [
        # Turn 1 reaches the cutoff (160000/400000 = 0.40), no complete_step.
        [thread_started("thr-1"), agent_message("working on it"),
         turn_completed(input_tokens=1_000)],
        # Turn 2: the agent writes the handoff as instructed (no complete_step);
        # the runner emits the terminal `handoff` at this turn's boundary.
        [agent_message("wrote the handoff"),
         turn_completed(input_tokens=1_000)],
    ]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"  # test seam

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog)

    events = [ev async for ev in runner.run(inv)]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    # The instruction was injected as an engine_message/continuation the agent acted on.
    engine_msgs = [e for e in elog.read() if e["type"] == "engine_message"]
    assert any("approaching the context limit" in str(e["payload"])
               for e in engine_msgs)
    assert hp.exists()
    assert hp.read_text() == "wrote the handoff"


@pytest.mark.anyio
async def test_silent_handoff_turn_does_not_write_note(
        tmp_path: Path, fake_codex_rollout_readings: list[RolloutReadings]) -> None:
    """When the handoff turn is agent-silent (no agent_message event), no note
    must be written. The handoff event must still fire so the engine reincarnates."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    hp = handoff_path_for(tmp_path / "tx")
    fake_codex_rollout_readings[0] = RolloutReadings(
        context=(160_000, 400_000), quota=None)

    scripted = [
        # Turn 1 reaches the cutoff (160000/400000 = 0.40).
        [thread_started("thr-1"), agent_message("working on it"),
         turn_completed(input_tokens=1_000)],
        # Turn 2: agent is completely silent (no agent_message).
        [turn_completed(input_tokens=1_000)],
    ]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog)

    events = [ev async for ev in runner.run(inv)]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    assert not hp.exists()


@pytest.mark.anyio
async def test_below_threshold_does_not_elicit_codex_handoff(tmp_path: Path) -> None:
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()

    from tests.codex_fakes import mcp_call
    scripted = [[
        thread_started("thr-1"), agent_message("done"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        # No rollout from FakeCodex -> no reading -> the watch cannot fire.
        turn_completed(input_tokens=100_000),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog)

    events = [ev async for ev in runner.run(inv)]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)


@pytest.mark.anyio
async def test_completing_turn_at_threshold_completes_not_handoff(
        tmp_path: Path, fake_codex_rollout_readings: list[RolloutReadings]) -> None:
    """Completion wins at the threshold: a turn whose reading reaches the
    cutoff (160000/400000 = 0.40) AND calls complete_step yields
    agent_result, never a handoff. Pins the ordering invariant (complete_step
    check precedes crossing detection) against a reordering regression — so the
    crossing must be real, not absent."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    fake_codex_rollout_readings[0] = RolloutReadings(
        context=(160_000, 400_000), quota=None)

    from tests.codex_fakes import mcp_call
    scripted = [[
        thread_started("thr-1"), agent_message("done"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        # 160000/400000 = 0.40 reaches the cutoff, but the turn completes.
        turn_completed(input_tokens=1_000),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog)

    events = [ev async for ev in runner.run(inv)]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)


@pytest.mark.anyio
async def test_rollout_reading_at_cutoff_elicits_handoff(
        tmp_path: Path, fake_codex_rollout_readings: list[RolloutReadings]) -> None:
    """The watch uses Codex's live reading: 160000/258400 reaches the preserved
    160k default-ceiling cutoff while this turn's 1,000-token billing sum does not."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    hp = handoff_path_for(tmp_path / "tx")
    fake_codex_rollout_readings[0] = RolloutReadings(
        context=(160_000, 258_400), quota=None)

    scripted = [
        [thread_started("thr-1"), agent_message("working on it"),
         turn_completed(input_tokens=1_000)],
        [agent_message("wrote the handoff"), turn_completed(input_tokens=1_000)],
    ]
    runner = CodexAgentRunner(
        model_name="gpt-5.6-luna",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex",
        model_name="gpt-5.6-luna",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog)

    events = [ev async for ev in runner.run(inv)]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    assert hp.read_text() == "wrote the handoff"


@pytest.mark.anyio
@pytest.mark.parametrize("reading", [(30_000, 258_400), None])
async def test_low_or_absent_reading_suppresses_handoff_on_a_huge_turn_sum(
        tmp_path: Path, fake_codex_rollout_readings: list[RolloutReadings],
        reading: tuple[int, int] | None) -> None:
    """A 300k aggregate billing sum exceeds the preserved 160k default ceiling,
    but Codex handoff uses the live rollout occupancy. A low or absent reading
    therefore lets the agent finish without a handoff."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    from tests.codex_fakes import mcp_call
    fake_codex_rollout_readings[0] = RolloutReadings(context=reading, quota=None)

    scripted = [
        [thread_started("thr-1"), agent_message("still working"),
         turn_completed(input_tokens=300_000)],
        [agent_message("done"),
         mcp_call(name="complete_step", args={"answer": "42"},
                  result="ok", item_id="cs-1"),
         turn_completed(input_tokens=1_000)],
    ]
    runner = CodexAgentRunner(
        model_name="gpt-5.6-luna",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex",
        model_name="gpt-5.6-luna",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog,
        continuation_fn=build_continuation_fn(headless=True, has_schema=True))

    events = [ev async for ev in runner.run(inv)]

    assert not [e for e in events if e["type"] == "handoff"]
    outs = [e for e in events if e["type"] == "agent_result"]
    assert len(outs) == 1 and outs[0]["payload"] == {"answer": "42"}
