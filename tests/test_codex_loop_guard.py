# tests/test_codex_loop_guard.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, agent_message, thread_started, turn_completed

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "i"], check=True)
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
        prompt_template_source="", source_path=Path("a.md"), source=_SRC, tier=None)
    return AgentInvocation(
        agent_def=adef, provider_name="codex", model_name="gpt-5.5",
        system_prompt="sp", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        # A non-self-limiting continuation_fn: the runner drives off
        # continuation_fn, so the run re-enters the turn loop on each
        # no-complete-step turn and the guard accumulates toward a trip.
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl", agent_id="step:t", event_log=elog,
        continuation_fn=lambda _last: "please call complete_step")


@pytest.mark.anyio
async def test_no_progress_loop_trips_loop_error(tmp_path: Path) -> None:
    auth = _seed(tmp_path)
    elog = InMemoryEventLog()
    # Repeated identical no-progress turns (text only, no complete_step, no
    # write-class tool) — more than the loop-guard's no-progress threshold.
    turn = [agent_message("still thinking"), turn_completed(output_tokens=1)]
    # repeat_k=3 identical turns are enough to trip; the extra turns are margin.
    scripted = [[thread_started("t"), *turn]] + [list(turn) for _ in range(8)]

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = auth

    events = [ev async for ev in runner.run(_inv(tmp_path, elog))]

    loop = [e for e in events if e.get("payload", {}).get("category") == "loop"]
    assert loop, events
    assert loop[0]["payload"]["kind"] == "repeated_failure"
