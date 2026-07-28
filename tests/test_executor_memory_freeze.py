"""Project memory freezes into state.json at first run and is reused on resume —
never re-read live mid-quest. `state.memory is None` is the freeze gate: None ==
not yet frozen, {} == frozen with no memory files."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.executor import RunResult, run_quest
from imp.engine.state import State
from tests.test_executor import _make_quest, _register_invocation_capturing


@pytest.fixture
def anyio_backend():
    return "asyncio"


_FAKE_CONFIG = (
    "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
)


def _seed(root: Path, *, memory: str | None = None):
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "hello world\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    if memory is not None:
        (root / "imp" / "memory").mkdir(parents=True, exist_ok=True)
        (root / "imp" / "memory" / "all.md").write_text(memory)
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_quest(root)


@pytest.mark.anyio
async def test_first_run_freezes_memory(tmp_path: Path) -> None:
    quest = _seed(tmp_path, memory="live note")
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_quest(quest, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    assert State.load(quest.state_dir).memory == {"all.md": "live note"}
    body = sink[0].initial_messages[0].content[0].text
    assert "## Project memory" in body            # engine-injected section
    assert "live note" in body                    # frozen memory text


@pytest.mark.anyio
async def test_project_with_no_memory_freezes_an_empty_snapshot(tmp_path: Path) -> None:
    quest = _seed(tmp_path)
    _register_invocation_capturing({"status": "Success"}, [])

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)
    # {} (frozen, no files), NOT None — otherwise the gate would re-freeze.
    assert State.load(quest.state_dir).memory == {}


@pytest.mark.anyio
async def test_first_run_raises_on_invalid_memory_and_leaves_state_unfrozen(
    tmp_path: Path,
) -> None:
    """An invalid imp/memory/ tree raises LoadError at the first-run freeze site
    and leaves state.memory None (freeze never committed)."""
    quest = _seed(tmp_path)
    (tmp_path / "imp" / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "memory" / "notallowed.md").write_text("x")

    with pytest.raises(LoadError):
        await run_quest(quest, on_event=lambda _e: None)
    assert State.load(quest.state_dir).memory is None


@pytest.mark.anyio
async def test_run_uses_frozen_memory_not_the_live_tree(tmp_path: Path) -> None:
    # The live imp/memory/all.md differs from what state was frozen with. An
    # already-frozen quest must render the FROZEN memory and must not re-freeze —
    # even though project_facts was never set.
    quest = _seed(tmp_path, memory="LIVE-MEMO")
    state = State.load(quest.state_dir)
    state.memory = {"all.md": "FROZEN-MEMO"}
    state.save(quest.state_dir)

    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)
    await run_quest(quest, on_event=lambda _e: None)

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)
    body = sink[0].initial_messages[0].content[0].text
    assert "## Project memory" in body
    assert "FROZEN-MEMO" in body                  # frozen memory, injected
    assert "LIVE-MEMO" not in body                # live tree never read
    assert State.load(quest.state_dir).memory == {"all.md": "FROZEN-MEMO"}
