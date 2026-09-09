from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import executor as executor_mod
from imp.engine.executor import RunResult, _refresh_workflow_snapshot, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.providers import BUILTIN_FLOOR
from tests.test_executor import _FakeProvider, _seed_minimal_workflow


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ok_factory(model: str) -> object:
    return _FakeProvider({"status": "Success"}, model=model)


def _floor_config(root: Path) -> None:
    """Configure claude_code as the whole provider order and name no tiers, so
    every tier model comes from the built-in floor — the shape a pin has to reach
    past, since codex is not in the configured list at all."""
    (project_state_dir(root) / "config.yaml").write_text("providers: [claude_code]\n")


def _reopen(quest: Quest) -> State:
    """The home as a fresh daemon would find it. A completed run archives the
    quest and reaps its home, and rehydrate_quest restores only quest.json,
    state.json, events.jsonl and the step blobs — never quest_dir/workflow — so
    the re-snapshot is what makes a later entry see a workflow at all."""
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)
    _refresh_workflow_snapshot(quest)
    return State.load(quest.state_dir)


@pytest.mark.anyio
async def test_an_unpinned_run_resolves_the_configured_vendor(
    tmp_path: Path, floor_availability,
) -> None:
    # The control: same config, same registry, no pin — every tier lands on the
    # configured claude_code, which is what the pinned run below must change.
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    floor_availability({"claude_code", "codex"}, factory=_ok_factory)

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    tiers = _reopen(quest).resolved_tiers
    assert {pm.provider for pm in tiers.values()} == {"claude_code"}


@pytest.mark.anyio
async def test_a_pinned_run_resolves_every_tier_off_the_pinned_vendors_floor(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    floor_availability({"claude_code", "codex"}, factory=_ok_factory)
    quest.set_provider("codex")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    tiers = _reopen(quest).resolved_tiers
    assert {t: (pm.provider, pm.model) for t, pm in tiers.items()} == {
        t: ("codex", model)
        for t, model in BUILTIN_FLOOR["codex"].tier_defaults.items()}


@pytest.mark.anyio
async def test_a_workflow_refresh_resolves_new_steps_on_the_pinned_vendor(
    tmp_path: Path, floor_availability,
) -> None:
    # Narrowing only the first-run resolve would let a mid-quest workflow refresh
    # reintroduce the un-pinned vendor for every step it adds.
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    floor_availability({"claude_code", "codex"}, factory=_ok_factory)
    await run_quest(quest, on_event=lambda _e: None)     # resolves onto claude_code
    _reopen(quest)
    quest.set_provider("codex")

    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
        "      - {name: w2, file: agents/writer.md, tier: high, differ_from: w}\n"
    )
    # A merged workflow change is adopted on a FRESH daemon, not a same-process
    # re-entry: clear the once-per-process gate to model that process restart.
    executor_mod._SNAPSHOT_CAPTURED.clear()

    await run_quest(quest, on_event=lambda _e: None)

    state = _reopen(quest)
    assert state.resolved_steps["w2"].provider == "codex"
    # Reconcile point (a) re-points the whole map to the pin at run entry, so
    # the refresh's new step and the standing tiers agree on the pinned vendor.
    assert state.resolved_tiers["low"].provider == "codex"


@pytest.mark.anyio
async def test_an_unresolvable_pin_is_refused_before_a_workflow_refresh_merge(
    tmp_path: Path, floor_availability,
) -> None:
    # The refresh merge must not out-run the refusal: it re-resolves against the
    # pin too, so a pin that can no longer resolve would raise there and kill the
    # resume instead of being refused and cleared.
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    floor_availability({"claude_code", "codex"}, factory=_ok_factory)
    await run_quest(quest, on_event=lambda _e: None)     # resolves onto claude_code
    _reopen(quest)
    quest.set_provider("codex")
    floor_availability({"claude_code"}, factory=_ok_factory)

    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
        "      - {name: w2, file: agents/writer.md, tier: high, differ_from: w}\n"
    )
    executor_mod._SNAPSHOT_CAPTURED.clear()

    events: list[dict] = []
    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert quest.provider is None
    state = _reopen(quest)
    assert state.resolved_steps["w2"].provider == "claude_code"
    assert json.loads((quest.quest_dir / "quest.json").read_text())["provider"] is None
    assert any(
        e["type"] == "script_output"
        and e["payload"]["stdout"].startswith(
            "couldn't switch to codex — staying on claude_code")
        for e in events)
