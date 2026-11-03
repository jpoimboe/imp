from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.cli import signals
from imp.cli.signals import InterruptCoordinator
from imp.engine.agent_run import AgentRunResult, drive_invocation
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import INTERRUPT_STEER_HINT
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.scripted import (
    InterruptMode,
    ScriptedAgentRunner,
    ScriptedProvider,
    _interrupt_mode,
)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="gate", description="",
        tools=frozenset({"bash", "complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, *, event_log: Any, user_input_fn: Any,
         coordinator: Any = None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="scripted", model_name="x",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=event_log,
        user_input_fn=user_input_fn,
        coordinator=coordinator,
    )


def _write_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> Path:
    marker = tmp_path / "running"
    script = {"interrupt": mode, "default": [
        {"text": "working",
         "tools": [{"name": "bash",
                    "input": {"command": f"touch {marker}; sleep 30"}}]},
        {"text": "done",
         "tools": [{"name": "complete_step",
                    "input": {"outcome": "ok", "status": "Success"}}]},
    ]}
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(path))
    return marker


async def _drive_and_interrupt(
    tmp_path: Path, marker: Path, mode: InterruptMode, user_input_fn: Any,
) -> tuple[AgentRunResult, InMemoryEventLog, float]:
    log = InMemoryEventLog()
    coord = InterruptCoordinator()
    coord.set_event_log(log)
    inv = _inv(tmp_path, event_log=log, user_input_fn=user_input_fn,
               coordinator=coord)
    runner = ScriptedAgentRunner("x", interrupt_mode=mode)
    result: list[AgentRunResult] = []

    async def _drive() -> None:
        result.append(await drive_invocation(
            inv, runner, coordinator=coord, log=log, emit_events=True))
        coord.note_yield()   # run_quest returning disarms the yield watchdog

    t0 = time.monotonic()
    with anyio.fail_after(25):
        async with anyio.create_task_group() as tg:
            coord.set_delivery_spawner(tg.start_soon)
            tg.start_soon(_drive)
            while not marker.exists():
                await anyio.sleep(0.05)
            await coord.signal_pause(reason="ctrl-c", via="frame")
    return result[0], log, time.monotonic() - t0


def _of(log: InMemoryEventLog, etype: str) -> list[dict[str, Any]]:
    return [e for e in log.read() if e["type"] == etype]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_cooperative_steer_redirects_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = _write_script(tmp_path, monkeypatch, "cooperative")
    res, log, elapsed = await _drive_and_interrupt(
        tmp_path, marker, "cooperative", lambda _q: "redirect please")

    assert res.output is not None and res.output["status"] == "Success"
    assert not res.paused
    awaiting = _of(log, "awaiting_user_input")
    assert len(awaiting) == 1
    assert awaiting[0]["payload"]["interrupt"] is True
    assert awaiting[0]["payload"]["steer_hint"] == INTERRUPT_STEER_HINT
    user_texts = [b.get("text") for e in _of(log, "user_message")
                  for b in e["payload"]["content"]]
    assert "redirect please" in user_texts
    delivered = _of(log, "interrupt_delivered")
    assert [(d["payload"]["tier"], d["payload"]["outcome"]) for d in delivered] == [
        ("cooperative", "ok")]
    assert not _of(log, "interrupt_escalated")
    assert elapsed < 10


@pytest.mark.anyio
async def test_cooperative_steer_leaving_client_pauses_awaiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def leave(_q: str) -> str:
        raise AwaitingUserInput()

    marker = _write_script(tmp_path, monkeypatch, "cooperative")
    res, log, _ = await _drive_and_interrupt(tmp_path, marker, "cooperative", leave)

    assert res.paused and res.awaiting_user and res.interrupt
    assert res.output is None
    assert not _of(log, "interrupt_escalated")


@pytest.mark.anyio
async def test_hard_kill_escalates_and_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signals, "_COOPERATIVE_DEADLINE_S", 0.3)
    marker = _write_script(tmp_path, monkeypatch, "hard_kill")
    res, log, elapsed = await _drive_and_interrupt(
        tmp_path, marker, "hard_kill", lambda _q: "unused")

    assert res.paused and res.interrupt
    assert res.output is None
    assert not res.awaiting_user
    esc = _of(log, "interrupt_escalated")
    assert [e["payload"]["reason"] for e in esc] == ["cooperative_timeout"]
    tiers = [(d["payload"]["tier"], d["payload"]["outcome"])
             for d in _of(log, "interrupt_delivered")]
    assert tiers == [("cooperative", "timeout"), ("hard", "ok")]
    assert not _of(log, "awaiting_user_input")
    assert [e["payload"]["kind"] for e in _of(log, "error")] == ["interrupted"]
    assert not _of(log, "agent_result")
    assert elapsed < 10


def test_interrupt_mode_parsing_and_default_has_no_force_kill() -> None:
    assert getattr(ScriptedAgentRunner("x"), "force_kill", None) is None
    assert getattr(ScriptedAgentRunner("x", "cooperative"), "force_kill", None) is None
    assert _interrupt_mode({}) == "default"
    assert _interrupt_mode({"interrupt": "cooperative"}) == "cooperative"
    assert _interrupt_mode({"interrupt": "hard_kill"}) == "hard_kill"
    assert ScriptedAgentRunner("x", "hard_kill").force_kill is not None
    with pytest.raises(ValueError, match="unknown interrupt mode 'bogus'"):
        _interrupt_mode({"interrupt": "bogus"})


def test_provider_wires_mode_from_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"interrupt": "cooperative", "default": []}))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(path))
    inv = _inv(tmp_path, event_log=InMemoryEventLog(), user_input_fn=None)
    runner = ScriptedProvider("x").create_runner(inv)
    assert isinstance(runner, ScriptedAgentRunner)
    assert runner.interrupt_mode == "cooperative"
    assert getattr(runner, "force_kill", None) is None
