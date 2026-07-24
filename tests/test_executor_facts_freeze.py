"""Project facts/memory freeze into state.json at first run and are reused on
resume — never re-read live mid-Quest (spec item 6 / AC 10)."""
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


def _seed(root: Path, *, facts_yaml: str, memory: str | None = None):
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "hello {{ project.greeting }}\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "project.yaml").write_text(facts_yaml)
    if memory is not None:
        (root / "imp" / "memory").mkdir(parents=True, exist_ok=True)
        (root / "imp" / "memory" / "all.md").write_text(memory)
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_quest(root)


@pytest.mark.anyio
async def test_first_run_freezes_facts_and_memory(tmp_path: Path) -> None:
    quest = _seed(
        tmp_path,
        facts_yaml="default-workflow: imp\nfacts:\n  greeting: hi\n",
        memory="live note",
    )
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_quest(quest, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    state = State.load(quest.state_dir)
    assert state.project_facts == {"greeting": "hi"}
    assert state.memory == {"all.md": "live note"}
    body = sink[0].initial_messages[0].content[0].text
    assert "hello hi" in body                     # fact still rendered via the template
    assert "## Project memory" in body            # engine-injected section
    assert "live note" in body                    # frozen memory text


@pytest.mark.anyio
async def test_first_run_raises_on_invalid_memory_and_leaves_state_unfrozen(
    tmp_path: Path,
) -> None:
    """An invalid imp/memory/ tree raises LoadError at the first-run freeze site
    and leaves state.project_facts None (freeze never committed)."""
    quest = _seed(
        tmp_path,
        facts_yaml="default-workflow: imp\nfacts:\n  greeting: hi\n",
    )
    (tmp_path / "imp" / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "memory" / "notallowed.md").write_text("x")

    with pytest.raises(LoadError):
        await run_quest(quest, on_event=lambda _e: None)
    assert State.load(quest.state_dir).project_facts is None


@pytest.mark.anyio
async def test_run_uses_frozen_state_not_live_project_yaml(tmp_path: Path) -> None:
    # project.yaml says greeting=hi and memory=live, but state was frozen earlier
    # with different values. The executor must render from the FROZEN state.
    quest = _seed(
        tmp_path,
        facts_yaml="default-workflow: imp\nfacts:\n  greeting: hi\n",
        memory="live",
    )
    state = State.load(quest.state_dir)
    state.project_facts = {"greeting": "FROZEN"}
    state.memory = {"all.md": "FROZEN-MEMO"}
    state.save(quest.state_dir)

    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)
    await run_quest(quest, on_event=lambda _e: None)

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    body = sink[0].initial_messages[0].content[0].text
    assert "hello FROZEN" in body                 # frozen fact, not live project.yaml
    assert "## Project memory" in body
    assert "FROZEN-MEMO" in body                  # frozen memory, injected
    assert "hello hi" not in body                 # live greeting never used
    # The guard never overwrites an already-frozen value.
    assert State.load(quest.state_dir).project_facts == {"greeting": "FROZEN"}
