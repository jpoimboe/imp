from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.providers import BUILTIN_FLOOR
from imp.providers.config import ProviderModel
from tests.test_executor import _seed_minimal_workflow
from tests.test_executor_provider_pin import _floor_config, _reopen


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_TWO_STEPS = (
    "name: t\ndescription: t\n"
    "defaults: {max_retries: 1, max_loops: 6}\n"
    "phases:\n"
    "  - name: only\n"
    "    steps:\n"
    "      - {name: w, file: agents/writer.md, tier: low}\n"
    "      - {name: w2, file: agents/writer.md, tier: low}\n"
)


def _recording(seen: list[str], arm: Quest | None = None):
    """A provider factory that records the model each invocation resolved to, and
    (optionally) arms a pin from inside the first invocation — exactly what the
    daemon's set_provider callback does to the live Quest mid-run."""

    class _Runner:
        # Public attribute only: tests/test_no_private_reachthroughs.py greps
        # the whole tree — comments included — for private runner state.
        def __init__(self, model: str) -> None:
            self.model = model

        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            seen.append(self.model)
            if arm is not None and len(seen) == 1:
                arm.set_provider("codex")
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "recording"

        def __init__(self, model: str) -> None:
            self.model = model

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _Runner(self.model)

    return _Provider


def _pre_resolved(quest: Quest, provider: str) -> None:
    state = State.load(quest.state_dir)
    state.resolved_tiers = {
        t: ProviderModel(provider=provider, model=m)
        for t, m in BUILTIN_FLOOR[provider].tier_defaults.items()}
    state.save(quest.state_dir)


@pytest.mark.anyio
async def test_run_entry_repoints_every_tier_before_the_first_dispatch(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    floor_availability({"claude", "codex"}, factory=_recording(seen))
    _pre_resolved(quest, "claude")
    quest.set_provider("codex")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    tiers = _reopen(quest).resolved_tiers
    assert {pm.provider for pm in tiers.values()} == {"codex"}
    # No invocation ran on the old vendor.
    assert seen == [BUILTIN_FLOOR["codex"].tier_defaults["low"]]


@pytest.mark.anyio
async def test_run_entry_discards_a_cross_vendor_step_override(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    floor_availability({"claude", "codex"}, factory=_recording([]))
    _pre_resolved(quest, "claude")
    state = State.load(quest.state_dir)
    state.resolved_steps = {"w": ProviderModel(provider="openai", model="gpt-6.1-sol")}
    state.save(quest.state_dir)
    quest.set_provider("codex")

    await run_quest(quest, on_event=lambda _e: None)

    assert "w" not in _reopen(quest).resolved_steps


@pytest.mark.anyio
async def test_a_pin_written_by_another_process_is_read_at_entry(
    tmp_path: Path, floor_availability,
) -> None:
    # A CLI that wrote the pin before the daemon spawned: this in-memory Quest
    # never saw it, so run_quest must re-read it onto the instance it holds.
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    floor_availability({"claude", "codex"}, factory=_recording(seen))
    _pre_resolved(quest, "claude")
    meta_path = quest.quest_dir / "quest.json"             # the other process
    meta = json.loads(meta_path.read_text())
    meta["provider"] = "codex"
    meta_path.write_text(json.dumps(meta, sort_keys=True))
    assert quest.provider is None                          # stale in memory

    await run_quest(quest, on_event=lambda _e: None)

    assert quest.provider == "codex"                       # assigned, not rebound
    assert seen == [BUILTIN_FLOOR["codex"].tier_defaults["low"]]


@pytest.mark.anyio
async def test_a_switch_armed_mid_run_lands_before_the_next_dispatch(
    tmp_path: Path, floor_availability,
) -> None:
    # Armed while step w runs (the daemon writing the live Quest); the step
    # boundary reconcile must apply it before w2 dispatches.
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(_TWO_STEPS)
    seen: list[str] = []
    floor_availability({"claude", "codex"},
                       factory=_recording(seen, arm=quest))
    _pre_resolved(quest, "claude")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    assert seen == [BUILTIN_FLOOR["claude"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]


@pytest.mark.anyio
async def test_the_confirmation_reaches_the_scrollback_seam(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    floor_availability({"claude", "codex"}, factory=_recording([]))
    _pre_resolved(quest, "claude")
    quest.set_provider("codex")
    events: list[dict[str, Any]] = []

    await run_quest(quest, on_event=events.append)

    assert {"type": "script_output", "payload": {
        "stdout": "provider switched — codex picks up this step from the event log",
    }} in events


@pytest.mark.anyio
async def test_a_run_with_nothing_pending_leaves_the_map_alone(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(_TWO_STEPS)
    seen: list[str] = []
    floor_availability({"claude", "codex"}, factory=_recording(seen))
    _pre_resolved(quest, "claude")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    low = BUILTIN_FLOOR["claude"].tier_defaults["low"]
    assert seen == [low, low]
    assert {pm.provider for pm in _reopen(quest).resolved_tiers.values()} == {
        "claude"}
    assert not [e for e in events
                if "provider switched" in str(e.get("payload", {}))]


@pytest.mark.anyio
async def test_pinning_the_vendor_already_resolved_is_a_no_op(
    tmp_path: Path, floor_availability,
) -> None:
    # A same-name pin resumes normally: no re-resolve, no confirmation, and
    # nothing for a marker to be left waiting on.
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    floor_availability({"claude", "codex"}, factory=_recording(seen))
    _pre_resolved(quest, "claude")
    quest.set_provider("claude")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen == [BUILTIN_FLOOR["claude"].tier_defaults["low"]]
    assert not [e for e in events
                if "provider switched" in str(e.get("payload", {}))]
