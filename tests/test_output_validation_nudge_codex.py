"""In-session nudge recovery for the codex provider (next_prompt re-drive)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.loop_guard import DEFAULT_REPEAT_K
from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, mcp_call, thread_started, turn_completed

pytestmark = pytest.mark.anyio
_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"],
                   check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "i"],
                   check=True)
    auth = tmp_path / "auth.json"
    auth.write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)
    return auth


def _inv(tmp_path: Path, elog: object) -> AgentInvocation:
    adef = AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC,
        tier=None)
    return AgentInvocation(
        agent_def=adef, provider_name="codex", model_name="gpt-5.5",
        system_prompt="sp", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl", agent_id="step:t", event_log=elog)


async def test_malformed_complete_step_nudged_then_recovers(
    tmp_path: Path,
) -> None:
    auth = _seed(tmp_path)
    elog = InMemoryEventLog()
    scripted = [
        [thread_started("thr-1"),
         mcp_call(name="complete_step", args={}, result="step complete"),
         turn_completed()],
        [thread_started("thr-1"),
         mcp_call(name="complete_step", args={"answer": "42"},
                  result="step complete"),
         turn_completed()],
    ]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(model_name="gpt-5.5",
                              client_factory=lambda options=None: fake)
    runner._canonical_auth_override = auth

    events = [e async for e in runner.run(_inv(tmp_path, elog))]

    outs = [e for e in events if e.get("type") == "agent_output"]
    assert len(outs) == 1 and outs[0]["payload"] == {"answer": "42"}
    log_types = [e["type"] for e in elog.read()]
    assert log_types.count("output_validation_nudge") == 1
    assert fake.thread is not None
    assert any("complete_step call was invalid" in str(p)
               for (p, _opt) in fake.thread.run_calls)


async def test_repeated_malformed_trips_loop_guard(tmp_path: Path) -> None:
    auth = _seed(tmp_path)
    elog = InMemoryEventLog()
    turn = [thread_started("thr-1"),
            mcp_call(name="complete_step", args={}, result="step complete"),
            turn_completed()]
    scripted = [list(turn) for _ in range(DEFAULT_REPEAT_K + 2)]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(model_name="gpt-5.5",
                              client_factory=lambda options=None: fake)
    runner._canonical_auth_override = auth

    events = [e async for e in runner.run(_inv(tmp_path, elog))]

    loop = [e for e in events if e.get("payload", {}).get("category") == "loop"]
    assert loop, events
    assert loop[-1]["payload"]["kind"] == "repeated_failure"
