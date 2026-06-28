from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.reincarnation import handoff_path_for
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
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
async def test_threshold_crossing_elicits_codex_handoff(tmp_path: Path) -> None:
    """A codex turn whose occupancy crosses 0.8 * 400000 = 320000 without
    completing the step elicits the handoff instruction as the next prompt; the
    following turn yields the terminal `handoff` event."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    hp = handoff_path_for(tmp_path / "tx")

    scripted = [
        # Turn 1: crosses (350000/400000 = 0.875) with no complete_step.
        [thread_started("thr-1"), agent_message("working on it"),
         turn_completed(input_tokens=350_000)],
        # Turn 2: the agent writes the handoff as instructed (no complete_step);
        # the runner emits the terminal `handoff` at this turn's boundary.
        [agent_message("wrote the handoff"),
         turn_completed(input_tokens=350_000)],
    ]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"  # test seam

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog, checkpoint_threshold=0.8)

    events = [ev async for ev in runner.run(inv)]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    # The instruction was injected as a user turn the agent then acted on.
    user_msgs = [e for e in elog.read() if e["type"] == "user_message"]
    assert any("approaching the context limit" in str(e["payload"])
               for e in user_msgs)


@pytest.mark.anyio
async def test_below_threshold_does_not_elicit_codex_handoff(tmp_path: Path) -> None:
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()

    from tests.codex_fakes import mcp_call
    scripted = [[
        thread_started("thr-1"), agent_message("done"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        # 100000/400000 = 0.25 < 0.8 -> no crossing.
        turn_completed(input_tokens=100_000),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog, checkpoint_threshold=0.8)

    events = [ev async for ev in runner.run(inv)]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_output" for e in events)


@pytest.mark.anyio
async def test_completing_turn_above_threshold_completes_not_handoff(
        tmp_path: Path) -> None:
    """Completion wins over a mere crossing: a turn that crosses the threshold
    (350000/400000 = 0.875) AND calls complete_step yields agent_output, never a
    handoff. Pins the ordering invariant (complete_step check precedes crossing
    detection) against a reordering regression."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()

    from tests.codex_fakes import mcp_call
    scripted = [[
        thread_started("thr-1"), agent_message("done"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        # 350000/400000 = 0.875 >= 0.8 -> crosses, but the turn completes.
        turn_completed(input_tokens=350_000),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog, checkpoint_threshold=0.8)

    events = [ev async for ev in runner.run(inv)]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_output" for e in events)
