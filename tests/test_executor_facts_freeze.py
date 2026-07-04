"""Project facts/guidance freeze into state.json at first run and are reused on
resume — never re-read live mid-Change (spec item 6 / AC 10)."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from tests.test_executor import _make_change, _register_invocation_capturing


@pytest.fixture
def anyio_backend():
    return "asyncio"


_FAKE_CONFIG = (
    "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
)


def _seed(root: Path, *, facts_yaml: str):
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
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_change(root)


@pytest.mark.anyio
async def test_first_run_freezes_facts_and_guidance_from_project_yaml(tmp_path: Path) -> None:
    change = _seed(
        tmp_path,
        facts_yaml="default-workflow: imp\nfacts:\n  greeting: hi\nguidance: live note\n",
    )
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.project_facts == {"greeting": "hi"}
    assert state.guidance == "live note"
    body = sink[0].initial_messages[0].content[0].text
    assert "hello hi" in body                     # fact still rendered via the template
    assert "## Project guidance" in body          # engine-injected section
    assert "live note" in body                    # frozen guidance text


@pytest.mark.anyio
async def test_run_uses_frozen_state_not_live_project_yaml(tmp_path: Path) -> None:
    # project.yaml says greeting=hi/guidance=live, but state was frozen earlier with
    # different values. The executor must render from the FROZEN state.
    change = _seed(
        tmp_path,
        facts_yaml="default-workflow: imp\nfacts:\n  greeting: hi\nguidance: live\n",
    )
    state = State.load(change.state_dir)
    state.project_facts = {"greeting": "FROZEN"}
    state.guidance = "FROZEN-GUIDANCE"
    state.save(change.state_dir)

    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)
    await run_change(change, on_event=lambda _e: None)

    body = sink[0].initial_messages[0].content[0].text
    assert "hello FROZEN" in body                 # frozen fact, not live project.yaml
    assert "## Project guidance" in body
    assert "FROZEN-GUIDANCE" in body              # frozen guidance, injected
    assert "hello hi" not in body                 # live greeting never used
    # The guard never overwrites an already-frozen value.
    assert State.load(change.state_dir).project_facts == {"greeting": "FROZEN"}
