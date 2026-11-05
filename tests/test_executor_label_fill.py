"""run_quest fills a parked quest's empty step and phase labels at entry, so a
quest that predates its workflow's labels shows them once it runs again."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from imp.cli import main
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.test_executor import _make_quest, _register_fake, _write_fake_config


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_conversation(root: Path, *, phase_label: str = "scoping phase") -> Quest:
    """One agent step that pauses on the fake provider's Error, so the quest stays on disk."""
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "chat.md").write_text(
        "---\nname: chat\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "vars:\n  n: {type: int, default: 7}\n"
        "phases:\n"
        "  - name: threshold\n"
        f"    label: {phase_label}\n"
        "    steps:\n"
        "      - name: threshold\n"
        "        file: agents/chat.md\n"
        "        tier: low\n"
        "        label: chatting\n"
        "        rules:\n"
        "          - {if: {status: Error}, pause: true}\n"
        "          - {else: true, continue: true}\n"
    )
    _write_fake_config(root)
    _register_fake({"status": "Error"})
    quest = _make_quest(root)
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta_path.write_text(json.dumps({**meta, "created_at": "2026-06-20T11:00:00+00:00"}))
    return quest


def _park(quest: Quest, **fields: object) -> None:
    state = State.load(quest.state_dir)
    state.status = "paused"
    state.has_started = True
    for name, value in fields.items():
        setattr(state, name, value)
    state.save(quest.state_dir)


async def _labels_when_step_starts(quest: Quest) -> tuple[str | None, str | None]:
    """The labels on disk at the first step_started, proving the fill preceded the step."""
    seen: list[tuple[str | None, str | None]] = []

    def _snap(event: dict) -> None:
        if event["type"] == "step_started" and not seen:
            state = State.load(quest.state_dir)
            seen.append((state.step_label, state.phase_label))

    assert await run_quest(quest, on_event=_snap) is RunResult.PAUSED
    return seen[0]


@pytest.mark.anyio
async def test_resume_fills_both_null_labels_before_the_step_runs(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path)
    _park(quest, step_label=None, phase_label=None)

    assert await _labels_when_step_starts(quest) == ("chatting", "scoping phase")

    lines = main._overview_lines(
        tmp_path, show_all=False, now=datetime.now(UTC), color=False)
    assert any(quest.id in line and "paused (scoping phase · chatting)" in line
               for line in lines)


@pytest.mark.anyio
async def test_pushed_step_label_survives_while_null_phase_label_fills(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path)
    _park(quest, step_label="task 3/5", phase_label=None)

    assert await _labels_when_step_starts(quest) == ("task 3/5", "scoping phase")


@pytest.mark.anyio
async def test_set_phase_label_survives_while_null_step_label_fills(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path)
    _park(quest, step_label=None, phase_label="threshold phase")

    assert await _labels_when_step_starts(quest) == ("chatting", "threshold phase")


@pytest.mark.anyio
async def test_null_step_label_fills_from_the_route_label(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path)
    _park(quest, step_label=None, route_label="writing the missing spec")

    step_label, _phase_label = await _labels_when_step_starts(quest)

    assert step_label == "writing the missing spec"


@pytest.mark.anyio
async def test_set_step_label_survives_a_pending_route_label(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path)
    _park(quest, step_label="task 3/5", route_label="writing the missing spec")

    step_label, _phase_label = await _labels_when_step_starts(quest)

    assert step_label == "task 3/5"


@pytest.mark.anyio
async def test_fill_renders_a_var_the_quest_only_gains_at_backfill(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path, phase_label="scoping phase {{ vars.n }}")
    _park(quest, vars={})

    _step_label, phase_label = await _labels_when_step_starts(quest)

    assert phase_label == "scoping phase 7"


@pytest.mark.anyio
async def test_step_absent_from_the_workflow_keeps_null_labels_and_pauses(tmp_path: Path) -> None:
    quest = _seed_conversation(tmp_path)
    _park(quest, step="ghost-step")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.PAUSED

    state = State.load(quest.state_dir)
    assert (state.step_label, state.phase_label) == (None, None)
    paused = [e for e in quest.event_log().read() if e["type"] == "workflow_paused"]
    assert paused[-1]["payload"]["reason"] == "unknown_step"
